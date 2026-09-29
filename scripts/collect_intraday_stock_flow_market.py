"""批量保存盘中全市场个股资金流，并提供可恢复的逐页检查点。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
import os
from pathlib import Path
import sys
import uuid

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.config import DB_PATH, TODAY, SETTINGS
from trade_system.tushare_history import TushareHistoryCollector
from trade_system.schema import init_schema
from trade_system.eastmoney_finance import (get_fund_flow_market, get_fund_flow_market_realtime,
    normalize_fund_flow_page as _normalize_page, normalize_realtime_flow_page as _normalize_realtime_page)
from trade_system.multi_source_store import MultiSourceStore


def _compact(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _number(value):
    from trade_system.units import _number as finite_number
    return finite_number(value)


def _collect_dc_snapshot(con, trade_date, universe, on_page, *, max_pages=None):
    """Dated relay snapshot; reuse a complete canonical receipt without refreshing it."""
    from trade_system.xiaodefa_source import XiaodefaClient
    if not universe:
        raise ValueError('dated A-share reference unavailable')
    # API limit is 6000; one full page alone cannot prove completeness.
    pages = []
    acquisition_id = uuid.uuid4().hex
    def retain(offset, rows):
        received = datetime.now()
        payload = json.dumps(dict(api='moneyflow_dc',params={'trade_date':_compact(trade_date)},
                                  acquisition_id=acquisition_id,offset=offset,rows=rows), ensure_ascii=False,sort_keys=True,allow_nan=False)
        con.execute("INSERT INTO multi_source_observation "
            "(source_date,data_type,asset_type,provider,status,payload_json,payload_hash,observed_at) "
            "VALUES (?,'tushare_moneyflow_dc','receipt','xiaodefa','received_unverified',?,?,?)",
            [trade_date,payload,hashlib.sha256(payload.encode()).hexdigest(),received])
        pages.append((rows, received.timestamp()))
    limit = min(3, int(max_pages)) if max_pages is not None else 3
    if limit < 1:
        raise ValueError('positive relay page budget required')
    # Re-project a complete retained page chain, preserving every arrival time.
    # Never assemble different acquisition attempts or reuse a truncated prefix.
    raw_reused = False
    chain = []
    next_offset = None
    chain_id = None
    for raw, digest, arrived in con.execute(
            "SELECT payload_json,payload_hash,observed_at FROM multi_source_observation "
            "WHERE data_type='tushare_moneyflow_dc' AND provider='xiaodefa' AND source_date=? "
            "AND observed_at<=current_timestamp ORDER BY observed_at DESC LIMIT 12", [trade_date]).fetchall():
        if hashlib.sha256(raw.encode()).hexdigest() != digest:
            chain, next_offset = [], None
            continue
        payload = json.loads(raw)
        batch, offset = payload.get('rows'), payload.get('offset')
        valid = (payload.get('api') == 'moneyflow_dc'
                 and payload.get('params') == {'trade_date':_compact(trade_date)}
                 and isinstance(batch,list) and type(offset) is int and offset >= 0 and offset % 6000 == 0
                 and arrived.date().isoformat() >= trade_date
                 and (arrived.date().isoformat() > trade_date or arrived.hour >= 15))
        if not valid:
            chain, next_offset = [], None
            continue
        if next_offset is None:
            if len(batch) >= 6000 or offset // 6000 >= limit:
                continue
            chain_id = payload.get('acquisition_id')
            # Legacy single pages are self-contained; a time interval alone
            # cannot establish that several pages share one acquisition.
            if offset and (not isinstance(chain_id, str) or not chain_id):
                continue
            chain = [(batch, arrived.timestamp())]
        elif (offset != next_offset or len(batch) != 6000
              or payload.get('acquisition_id') != chain_id or chain[0][1]-arrived.timestamp() > 60):
            chain, next_offset = [], None
            continue
        else:
            chain.append((batch, arrived.timestamp()))
        next_offset = offset - 6000
        if offset == 0:
            pages = list(reversed(chain))
            rows = [row for batch, _ in pages for row in batch]
            raw_reused = bool(rows)
            break
    if not raw_reused:
        rows = XiaodefaClient(timeout=60,max_retries=1).query_all('moneyflow_dc',
            trade_date=_compact(trade_date),page_size=6000,max_rows=6000*limit,on_page=retain)
    identities = [r.get('ts_code') for r in rows]
    if not rows:
        raise ValueError('dated relay stock flow unavailable: empty response')
    if (len(set(identities)) != len(rows) or any(
            r.get('trade_date') != _compact(trade_date) or not isinstance(r.get('ts_code'), str)
            or len(r['ts_code']) != 9 or not r['ts_code'][:6].isascii() or not r['ts_code'][:6].isdigit() or r['ts_code'][-3:] not in ('.SH','.SZ','.BJ') for r in rows)):
        raise ValueError('duplicate identity or wrong-date relay stock flow')
    # A returned A-share absent from the reference is a reconciliation item,
    # never permission to silently discard it and report 100% coverage.
    unknown = sorted({r['ts_code'] for r in rows if (code := r['ts_code'])[:6] not in universe
                      and ((_number(r.get('close')) or 0) > 0 or (_number(r.get('net_amount')) or 0) != 0)
                      and not (code.endswith('.SH') and code.startswith('9'))
                      and not (code.endswith('.SZ') and code.startswith('2'))})
    if len(unknown) > 20:
        raise ValueError('stock reference discrepancy exceeds bounded repair budget')
    additions = {}
    from trade_system.http_transport import request_budget
    with request_budget(30):
        for code in unknown:
            evidence = TushareHistoryCollector.stock_listing_evidence(con, code)
            items = evidence.get('item', [])
            listed = items[0].get('list_date') if len(items) == 1 else None
            if (len(items) != 1 or items[0].get('thscode') != code
                    or items[0].get('asset_type') != 'a-share' or not listed
                    or not date(1990,1,1) <= date.fromisoformat(listed) <= date.fromisoformat(trade_date)):
                raise ValueError('unresolved returned stock identity: '+code)
            additions[code[:6]] = code[-2:]
    universe.update(additions)
    for index, (batch, received_at) in enumerate(pages, 1):
        normalized = [dict(code=r['ts_code'][:6],date=trade_date,main_net=r.get('net_amount'),
            super_net=r.get('buy_elg_amount'),large_net=r.get('buy_lg_amount'),
            mid_net=r.get('buy_md_amount'),small_net=r.get('buy_sm_amount'),
            close=r.get('close'),change_pct=r.get('pct_change'),raw=r,
            amount_unit='10000_yuan',flow_definition='provider_main_orders_net',
            source_api='moneyflow_dc',origin_provider='eastmoney') for r in batch
            if r['ts_code'] == f"{r['ts_code'][:6]}.{universe.get(r['ts_code'][:6])}"]
        on_page(index,normalized,len(pages),dict(count=len(rows),received_at=received_at,receipt_reused=raw_reused))
    return rows, dict(source='xiaodefa_moneyflow_dc',pages=len(pages),expected_rows=len(universe),
                     receipt_reused=raw_reused,raw_receipt_reused=raw_reused, verified_universe_additions=additions)


def _ensure_checkpoint_table(con: duckdb.DuckDBPyConnection) -> None:
    if os.environ.get("KPL_RUNTIME_SCHEMA_READY", "").strip() == "1":
        return
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_stock_flow_batch (
            trade_date DATE PRIMARY KEY,
            run_id VARCHAR,
            provider VARCHAR,
            expected_rows INTEGER,
            fetched_rows INTEGER,
            expected_pages INTEGER,
            fetched_pages INTEGER,
            coverage_pct DOUBLE,
            status VARCHAR,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute("ALTER TABLE intraday_stock_flow_batch ADD COLUMN IF NOT EXISTS source_rows INTEGER")
    con.execute("ALTER TABLE intraday_stock_flow_batch ADD COLUMN IF NOT EXISTS unavailable_rows INTEGER")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_stock_flow_reconciliation (
            trade_date DATE PRIMARY KEY,
            primary_provider VARCHAR,
            primary_rows INTEGER,
            reference_provider VARCHAR,
            reference_rows INTEGER,
            overlap_rows INTEGER,
            primary_only_rows INTEGER,
            reference_only_rows INTEGER,
            overlap_reference_pct DOUBLE,
            overlap_sign_disagreement INTEGER DEFAULT 0,
            overlap_sign_disagreement_pct DOUBLE,
            mean_abs_main_net_diff DOUBLE,
            value_status VARCHAR DEFAULT 'not_observed',
            status VARCHAR,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    for column, column_type in (
        ("overlap_sign_disagreement", "INTEGER DEFAULT 0"),
        ("overlap_sign_disagreement_pct", "DOUBLE"),
        ("mean_abs_main_net_diff", "DOUBLE"),
        ("value_status", "VARCHAR DEFAULT 'not_observed'"),
    ):
        con.execute(
            f"ALTER TABLE intraday_stock_flow_reconciliation "
            f"ADD COLUMN IF NOT EXISTS {column} {column_type}"
        )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_stock_flow_page_checkpoint (
            trade_date DATE,
            page_no INTEGER,
            pages_expected INTEGER,
            status VARCHAR,
            rows_written INTEGER DEFAULT 0,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(trade_date, page_no)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_stock_flow_missing (
            trade_date DATE,
            stock_code VARCHAR,
            provider VARCHAR,
            reason VARCHAR,
            detected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(trade_date, stock_code, provider)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_stock_flow_exchange_coverage (
            trade_date DATE,
            exchange VARCHAR,
            provider VARCHAR,
            expected_rows INTEGER,
            fetched_rows INTEGER,
            coverage_pct DOUBLE,
            status VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(trade_date, exchange, provider)
        )
        """
    )


def _a_share_universe_by_exchange(con: duckdb.DuckDBPyConnection, trade_date=None) -> dict[str, str]:
    """Return the current point-in-time A-share universe used as denominator.

    B shares are exchange-specific: Shanghai B shares start with 9 and
    Shenzhen B shares start with 2.  Beijing A shares also start with 9, so a
    prefix-only exclusion incorrectly removes the whole Beijing market.
    """
    try:
        day = trade_date or date.today().isoformat()
        with TushareHistoryCollector(':memory:', offline=True, connection=con) as reference:
            qualified = reference._reference_version()
            if os.environ.get('KPL_RUNTIME_SCHEMA_READY') == '1' and not qualified:
                return {}
            # A missing native listing date needs current official membership,
            # never an invented IPO date or an all-time instrument denominator.
            if not qualified and con.execute('SELECT 1 FROM tushare_stock_basic WHERE list_date IS NULL LIMIT 1').fetchone():
                return {}
            expected = reference._expected_stock_codes(day)
        rows = con.execute(
            """
            SELECT DISTINCT stock_code,
                   CASE
                     WHEN upper(CAST(ts_code AS VARCHAR)) LIKE '%.BJ' THEN 'BJ'
                     WHEN upper(CAST(ts_code AS VARCHAR)) LIKE '%.SH' THEN 'SH'
                     WHEN upper(CAST(ts_code AS VARCHAR)) LIKE '%.SZ' THEN 'SZ'
                   END AS exchange
            FROM tushare_stock_basic
            WHERE stock_code IS NOT NULL
              AND regexp_matches(CAST(stock_code AS VARCHAR), '^[0-9]{6}$')
              AND (upper(CAST(ts_code AS VARCHAR)) LIKE '%.SH'
                   OR upper(CAST(ts_code AS VARCHAR)) LIKE '%.SZ'
                   OR upper(CAST(ts_code AS VARCHAR)) LIKE '%.BJ')
              AND NOT (
                    upper(CAST(ts_code AS VARCHAR)) LIKE '%.SH'
                    AND CAST(stock_code AS VARCHAR) LIKE '9%'
              )
              AND NOT (
                    upper(CAST(ts_code AS VARCHAR)) LIKE '%.SZ'
                    AND CAST(stock_code AS VARCHAR) LIKE '2%'
              )
            """
        ).fetchall()
        return {str(code): str(exchange) for code, exchange in rows
                if code and exchange and f'{code}.{exchange}' in expected}
    except Exception:
        return {}


def _write_exchange_coverage(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    provider: str,
    universe: dict[str, str],
) -> dict[str, dict]:
    """Persist an honest per-exchange denominator for operator audits."""
    fetched_codes = {
        str(row[0])
        for row in con.execute(
            "SELECT DISTINCT stock_code FROM multi_source_stock_flow "
            "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE "
            "AND isfinite(main_net)",
            [trade_date, provider],
        ).fetchall()
    }
    result: dict[str, dict] = {}
    con.execute(
        "DELETE FROM intraday_stock_flow_exchange_coverage "
        "WHERE trade_date=CAST(? AS DATE) AND provider=?",
        [trade_date, provider],
    )
    for exchange in ("SH", "SZ", "BJ"):
        expected_codes = {code for code, value in universe.items() if value == exchange}
        fetched = len(expected_codes & fetched_codes)
        expected = len(expected_codes)
        coverage = round(fetched * 100.0 / expected, 2) if expected else 0.0
        status = (
            "success" if expected > 0 and fetched >= expected
            else "success_with_unavailable" if expected > 0 and coverage >= 99.5
            else "partial" if fetched else "error"
        )
        con.execute(
            "INSERT INTO intraday_stock_flow_exchange_coverage "
            "(trade_date,exchange,provider,expected_rows,fetched_rows,coverage_pct,status,updated_at) "
            "VALUES (CAST(? AS DATE),?,?,?,?,?,?,current_timestamp)",
            [trade_date, exchange, provider, expected, fetched, coverage, status],
        )
        result[exchange] = {
            "expected_rows": expected,
            "fetched_rows": fetched,
            "coverage_pct": coverage,
            "status": status,
        }
    return result


def collect_market_stock_flow(db_path: str | Path, trade_date: str, *, page_size: int = 500,
                              pause_seconds: float = 0.35, resume: bool = False,
                              max_pages: int | None = None,
                              crosscheck_after_close: bool = True, phase: str | None = None) -> dict:
    run_id = f"em_market_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    multi_store = MultiSourceStore(db_path)
    con = multi_store.con
    init_schema(con)
    _ensure_checkpoint_table(con)
    universe_by_exchange = _a_share_universe_by_exchange(con, trade_date)
    reference_error = ''
    if not universe_by_exchange:
        try:
            failure = con.execute("SELECT status,last_error,updated_at FROM history_fetch_checkpoint "
                "WHERE dataset='stock_basic' AND page_no=0 ORDER BY updated_at DESC LIMIT 1").fetchone()
            if failure:
                reference_error = f'preparation status={failure[0]} at={failure[2]}: {failure[1] or "dated reference not qualified"}'
        except duckdb.Error:
            reference_error = 'reference checkpoint unavailable'
    # Reference acquisition belongs to pre-session preparation, never this hot path.
    expected_universe = set(universe_by_exchange)
    previous_batch = con.execute(
        "SELECT expected_rows,expected_pages,fetched_rows,fetched_pages,provider,coverage_pct "
        "FROM intraday_stock_flow_batch WHERE trade_date=?",
        [trade_date],
    ).fetchone()
    now = datetime.now()
    if phase not in {None, 'intraday', 'close', 'supplemental'}:
        raise ValueError('explicit supported collection phase required')
    after_close = phase in {'close', 'supplemental'} if phase else (trade_date < date.today().isoformat() or (now.hour, now.minute) >= (15, 5))
    source_provider = "eastmoney_market" if after_close else "eastmoney_intraday_clist"
    if after_close and (SETTINGS.get('XIAODEFA_TOKEN') or SETTINGS.get('TUSHARE_XIAODEFA_TOKEN')):
        source_provider = 'xiaodefa_moneyflow_dc'
    completed_resume = False
    skip_pages = set()
    resume_start_page = 1
    if resume:
        skip_pages = {
            int(row[0]) for row in con.execute(
                "SELECT page_no FROM intraday_stock_flow_page_checkpoint WHERE trade_date=? AND status='success'",
                [trade_date],
            ).fetchall()
        }
        # A page checkpoint only proves that rows were written, not that the
        # page's codes were unique in the final universe.  If the previous
        # batch is below its denominator, replay every page so a moving
        # provider sort cannot leave an unrecoverable duplicate-only gap.
        if previous_batch and int(previous_batch[0] or 0) > 0:
            prior_fetched = int(previous_batch[2] or 0)
            prior_pages = int(previous_batch[3] or 0)
            prior_coverage = float(previous_batch[5] or 0)
            completed_resume = (
                prior_pages >= int(previous_batch[1] or 0) > 0
                and prior_coverage >= 99.5 and previous_batch[4] == source_provider
            )
            if prior_fetched < int(previous_batch[0]) and not completed_resume:
                skip_pages = set()
            if completed_resume:
                source_provider = str(previous_batch[4] or source_provider)
        resume_start_page = max(skip_pages) + 1 if skip_pages else 1
    else:
        # Keep the last valid snapshot until the first page of this refresh
        # has produced usable rows.  A remote reset before page 1 must not
        # erase yesterday's auditable partial/current snapshot.
        pass
    con.execute(
        "INSERT INTO intraday_stock_flow_batch(trade_date,run_id,provider,status,updated_at) VALUES (?,?,?,?,current_timestamp) "
        "ON CONFLICT(trade_date) DO UPDATE SET run_id=excluded.run_id,provider=excluded.provider,status=excluded.status,last_error=NULL,updated_at=excluded.updated_at",
        [trade_date, run_id, source_provider, "running"],
    )
    con.commit()
    written_pages = set()
    page_rows_seen = 0
    parsed_count, pending_writes, committed_writes = 0, 0, 0
    expected_pages = 0
    expected_rows = 0
    error = ""
    refresh_cleared = bool(resume)
    accepted_codes: set[str] = set()
    # A fresh snapshot is published atomically.  Page checkpoints/resume mode
    # intentionally remains incremental, but a normal refresh must never
    # delete yesterday's valid rows and leave a half-fetched current snapshot.
    atomic_refresh = not resume
    if resume:
        if previous_batch:
            expected_rows = int(previous_batch[0] or 0)
            expected_pages = int(previous_batch[1] or 0)
    elif previous_batch:
        # Preserve the last known denominator when a fresh refresh is blocked
        # before page 1; never report 0/0 for an auditable prior snapshot.
        expected_rows = int(previous_batch[0] or 0)
        expected_pages = int(previous_batch[1] or 0)

    def on_page(page_no: int, raw_rows: list[dict], pages: int, result: dict) -> None:
        nonlocal page_rows_seen, expected_pages, expected_rows, source_provider, refresh_cleared, accepted_codes
        nonlocal parsed_count, pending_writes, committed_writes
        page_provider = str(result.get("source") or source_provider)
        page_status = str(result.get("status") or ("live" if page_provider != "eastmoney_intraday_clist_delay" else "delayed"))
        # If the primary front door failed after one or more pages, the delayed
        # route restarts from page one.  Remove both partial providers before
        # accepting that fallback so one logical batch has one denominator.
        provider_changed = page_provider != source_provider
        if provider_changed:
            source_provider = page_provider
            # In a fresh atomic refresh, keep the old provider snapshot until
            # the replacement pass has completed.  DuckDB unique indexes do
            # not allow delete-and-reinsert of the same key inside one
            # transaction.  Non-atomic resume mode can still clear and commit
            # immediately because it intentionally publishes incrementally.
            if not atomic_refresh:
                con.execute("DELETE FROM intraday_stock_flow_page_checkpoint WHERE trade_date=?", [trade_date])
                con.execute(
                    "DELETE FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) "
                    "AND provider IN ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay')",
                    [trade_date],
                )
            con.execute(
                "UPDATE intraday_stock_flow_batch SET provider=?,updated_at=current_timestamp WHERE trade_date=?",
                [source_provider, trade_date],
            )
            if not atomic_refresh:
                con.commit()
            written_pages.clear()
            accepted_codes.clear()
            refresh_cleared = True
        expected_pages = int(pages or 0)
        expected_rows = len(expected_universe) or int(result.get("count") or 0)
        if page_no in skip_pages:
            written_pages.add(page_no)
            return
        rows = (raw_rows if source_provider == 'xiaodefa_moneyflow_dc' else _normalize_realtime_page(raw_rows, trade_date)
                if source_provider in {"eastmoney_intraday_clist", "eastmoney_intraday_clist_delay"}
                else _normalize_page(raw_rows, trade_date))
        parsed_count += len(rows)
        # The live clist contains B shares and can repeat rows at page
        # boundaries while its sort order moves.  The project contract is the
        # canonical A-share universe; filter before persistence and suppress
        # cross-page duplicates so the unique business key remains meaningful.
        rows = [row for row in rows if _number(row.get("main_net")) is not None]
        if expected_universe:
            rows = [row for row in rows if str(row.get("code") or "") in expected_universe]
        if not resume and not refresh_cleared and rows:
            if not atomic_refresh:
                con.execute("DELETE FROM intraday_stock_flow_page_checkpoint WHERE trade_date=?", [trade_date])
                con.execute(
                    "DELETE FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) AND provider IN ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay')",
                    [trade_date],
                )
            if not atomic_refresh:
                con.commit()
            accepted_codes.clear()
            refresh_cleared = True
        if atomic_refresh:
            rows = [row for row in rows if str(row.get("code") or "") not in accepted_codes]
            accepted_codes.update(str(row.get("code") or "") for row in rows if row.get("code"))
        page_rows_seen += len(rows)
        stored = multi_store.store(
            "stock_flow", None, rows,
            {"source": source_provider, "status": page_status, "trade_date": trade_date,
             "page_no": page_no, "pages": pages, "receipt_reused": bool(result.get("receipt_reused")), **({'received_at': result['received_at']} if 'received_at' in result else {})},
            asset_type="stock", trade_date=trade_date, commit=not atomic_refresh,
        )
        con.execute(
            "INSERT INTO intraday_stock_flow_page_checkpoint(trade_date,page_no,pages_expected,status,rows_written,last_error,updated_at) "
            "VALUES (?,?,?,?,?,?,current_timestamp) ON CONFLICT(trade_date,page_no) DO UPDATE SET pages_expected=excluded.pages_expected,status=excluded.status,rows_written=excluded.rows_written,last_error=excluded.last_error,updated_at=excluded.updated_at",
            [trade_date, page_no, pages, "success" if rows and stored.get("status") in {"live","refreshed","fresh","delayed"} else "empty",
             int(stored.get("rows_written", 0)), ""],
        )
        if not atomic_refresh:
            con.commit()
            committed_writes += int(stored['rows_written'])
        else:
            pending_writes += int(stored['rows_written'])
        written_pages.add(page_no)

    buffered_pages = []

    def buffer_page(page_no, raw_rows, pages, result):
        # Network budgets cover acquisition; canonical writes run afterwards.
        # Keep actual page arrival, never the later transaction time.
        buffered_pages.append((page_no, raw_rows, pages,
                               dict(result, received_at=result.get('received_at', datetime.now().timestamp()))))

    try:
        if os.environ.get('KPL_RUNTIME_SCHEMA_READY') == '1' and not expected_universe:
            raise ValueError('qualified dated A-share reference unavailable; collection not started; '+reference_error)
        dc_pages = []
        if source_provider == 'xiaodefa_moneyflow_dc':
            # Old completion used a potentially stale universe. Recheck the
            # retained raw receipt before trusting its previous 100% label.
            completed_resume = False
            skip_pages.clear()
        if source_provider == 'xiaodefa_moneyflow_dc' and not completed_resume:
            # Retain raw pages before canonical transaction, including failed
            # pagination. No second connection and no receipt timestamp rewrite.
            rows, meta = _collect_dc_snapshot(con, trade_date, universe_by_exchange,
                lambda *args: dc_pages.append(args), max_pages=max_pages)
            expected_universe = set(universe_by_exchange)
        if completed_resume:
            rows, meta = [], {
                "source": source_provider,
                "pages": int(previous_batch[1] or 0),
                "expected_rows": int(previous_batch[0] or 0),
                "rows": int(previous_batch[2] or 0),
                "status": "existing_complete_pages",
            }
        elif source_provider == 'xiaodefa_moneyflow_dc':
            buffered_pages.extend(dc_pages)
        elif source_provider in {"eastmoney_intraday_clist", "eastmoney_intraday_clist_delay"}:
            # During the session the datacenter/report endpoint is commonly
            # one session behind.  Use Eastmoney's live clist route directly
            # until 15:05; close runs use dated provider responses.
            rows, meta = get_fund_flow_market_realtime(
                trade_date, page_size=min(page_size, 100), max_pages=max_pages,
                pause_seconds=pause_seconds, on_page=on_page if resume else buffer_page,
                start_page=resume_start_page,
            )
        else:
            rows, meta = get_fund_flow_market(
                trade_date, page_size=page_size, max_pages=max_pages,
                pause_seconds=pause_seconds, on_page=on_page if resume else buffer_page,
            )
        expected_pages = int(meta.get("pages") or expected_pages or 0)
        expected_rows = len(expected_universe) or int(meta.get("expected_rows") or expected_rows or 0)
        if atomic_refresh:
            con.execute("BEGIN TRANSACTION")
        for page in buffered_pages:
            on_page(*page)
        source_provider = str(meta.get("source") or source_provider)
        if atomic_refresh and expected_pages > len(written_pages):
            raise ValueError('incomplete pagination; previous canonical snapshot retained')
        if (atomic_refresh and accepted_codes and previous_batch
                and len(accepted_codes) < int(previous_batch[2] or 0)):
            raise ValueError('shorter refresh; previous canonical snapshot retained')
        if atomic_refresh and accepted_codes:
            accepted_list = sorted(accepted_codes)
            # Publish exactly one provider snapshot.  Incoming keys have
            # already been updated/inserted; remove obsolete provider rows and
            # codes only after the complete paginated call returns.
            con.execute(
                "DELETE FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) "
                "AND provider IN ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay') "
                "AND provider<>?",
                [trade_date, source_provider],
            )
            con.execute(
                "DELETE FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) "
                "AND provider=? AND stock_code NOT IN (SELECT unnest(?::VARCHAR[]))",
                [trade_date, source_provider, accepted_list],
            )
            if written_pages:
                con.execute(
                    "DELETE FROM intraday_stock_flow_page_checkpoint WHERE trade_date=? "
                    "AND page_no NOT IN (SELECT unnest(?::INTEGER[]))",
                    [trade_date, sorted(written_pages)],
                )
        # Rows may have been skipped only in resume mode; the database is the
        # authoritative coverage count, not the in-memory response length.
        fetched_rows = int(con.execute(
            "SELECT count(DISTINCT stock_code) FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE AND isfinite(main_net)",
            [trade_date, source_provider],
        ).fetchone()[0])
        fetched_pages = int(con.execute(
            "SELECT count(*) FROM intraday_stock_flow_page_checkpoint WHERE trade_date=? AND status='success'",
            [trade_date],
        ).fetchone()[0])
        coverage = round((fetched_rows * 100.0 / expected_rows), 2) if expected_rows else 0.0
        # A positive row count without a provider denominator is not a proven
        # full snapshot (for example a resume probe after an empty response).
        # Keep it partial until the live endpoint supplies ``total``.
        # Full-market stock flow is an execution input: require the exact
        # provider denominator, not a 95% approximation that can hide a
        # missing page/universe slice.
        status = "success" if expected_rows > 0 and fetched_rows >= expected_rows else (
            "success_with_unavailable"
            if expected_rows > 0 and fetched_pages >= expected_pages > 0 and coverage >= 99.5
            else "partial" if fetched_rows else "error"
        )
        missing_codes = sorted(expected_universe - {
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT stock_code FROM multi_source_stock_flow "
                "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE AND isfinite(main_net)",
                [trade_date, source_provider],
            ).fetchall()
        }) if expected_universe else []
        con.execute("DELETE FROM intraday_stock_flow_missing WHERE trade_date=?", [trade_date])
        if missing_codes:
            con.executemany(
                "INSERT INTO intraday_stock_flow_missing(trade_date,stock_code,provider,reason) VALUES (?,?,?,?)",
                [[trade_date, code, source_provider, "not_returned_by_provider"] for code in missing_codes],
            )
    except Exception as exc:
        if atomic_refresh:
            pending_writes = 0
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        error = str(exc)[:500]
        fetched_rows = int(con.execute(
            "SELECT count(DISTINCT stock_code) FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE AND isfinite(main_net)",
            [trade_date, source_provider],
        ).fetchone()[0])
        fetched_pages = int(con.execute(
            "SELECT count(*) FROM intraday_stock_flow_page_checkpoint WHERE trade_date=? AND status='success'",
            [trade_date],
        ).fetchone()[0])
        coverage = round((fetched_rows * 100.0 / expected_rows), 2) if expected_rows else 0.0
        status = "partial" if fetched_rows else "error"
        missing_codes = sorted(expected_universe - {
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT stock_code FROM multi_source_stock_flow "
                "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE AND isfinite(main_net)",
                [trade_date, source_provider],
            ).fetchall()
        }) if expected_universe else []
    # Publish the primary full-market checkpoint before the optional after-close
    # reconciliation.  Reconciliation used to run first, so an OOM/fatal DuckDB
    # error while writing the reference snapshot left an otherwise complete
    # 99.5%+ primary batch permanently stuck at ``running``.
    con.execute(
        "UPDATE intraday_stock_flow_batch SET expected_rows=?,source_rows=?,unavailable_rows=?,fetched_rows=?,"
        "expected_pages=?,fetched_pages=?,coverage_pct=?,status=?,last_error=?,provider=?,updated_at=current_timestamp "
        "WHERE trade_date=?",
        [expected_rows, expected_rows, max(0, expected_rows - fetched_rows), fetched_rows,
         expected_pages, fetched_pages, coverage, status, error, source_provider, trade_date],
    )
    exchange_coverage = _write_exchange_coverage(
        con, trade_date, source_provider, universe_by_exchange
    ) if universe_by_exchange else {}
    con.commit()

    from trade_system.collection_profiles import emit_product_counts
    emit_product_counts('multi_source_stock_flow.primary', rows_parsed=parsed_count,
                        rows_written=committed_writes + pending_writes)

    # Controlled checkpoint while no reconciliation write is pending.  After a
    # full-market batch the WAL crosses DuckDB's auto-checkpoint threshold, so
    # the first subsequent write (historically the reconciliation INSERT)
    # triggered an implicit checkpoint that OOM'd and invalidated the
    # connection mid-reconciliation (2026-07-29), crashing the close chain.
    # Isolating the checkpoint here makes that pressure survivable: the
    # primary batch is already durable, so on failure we skip the optional
    # reconciliation instead of taking down the run.
    checkpoint_ok = True
    checkpoint_error = ""
    try:
        con.execute("CHECKPOINT")
    except Exception as exc:
        checkpoint_ok = False
        checkpoint_error = f"checkpoint failed: {str(exc)[:300]}"

    reconciliation = {
        "status": "not_run", "reference_provider": "eastmoney_market",
        "reference_rows": 0, "overlap_rows": 0, "primary_only_rows": 0,
        "reference_only_rows": 0, "overlap_reference_pct": 0.0,
        "overlap_sign_disagreement": 0, "overlap_sign_disagreement_pct": None,
        "mean_abs_main_net_diff": None, "value_status": "not_observed",
    }
    if not checkpoint_ok:
        reconciliation["status"] = "skipped"
        reconciliation["error"] = checkpoint_error
        # `con` is invalidated after a fatal checkpoint error; record the skip
        # on a fresh connection so the health dashboard shows why recon is
        # absent, then leave the invalidated connection untouched.
        try:
            from trade_system.db_utils import legacy_connect
            _skip_con = legacy_connect(str(db_path))
            _skip_con.execute(
                "INSERT INTO intraday_stock_flow_reconciliation "
                "(trade_date, primary_provider, primary_rows, status, value_status, last_error, updated_at) "
                "VALUES (?,?,?,?,?,?,current_timestamp) ON CONFLICT(trade_date) DO UPDATE SET "
                "status=excluded.status, last_error=excluded.last_error, updated_at=excluded.updated_at",
                [trade_date, source_provider, fetched_rows, "skipped", "not_observed", checkpoint_error],
            )
            _skip_con.commit()
            _skip_con.close()
        except Exception:
            pass
    is_after_close = datetime.now().hour > 15 or (datetime.now().hour == 15 and datetime.now().minute >= 5)
    if (checkpoint_ok and status.startswith("success") and trade_date == date.today().isoformat()
            and source_provider in {'xiaodefa_moneyflow_dc','eastmoney_intraday_clist','eastmoney_intraday_clist_delay'}
            and crosscheck_after_close and is_after_close and max_pages is None):
        try:
            reference_rows = [dict(code=code,main_net=value) for code,value in con.execute(
                "SELECT stock_code,main_net FROM multi_source_stock_flow WHERE source_date=? "
                "AND provider='eastmoney_market' AND is_stale=FALSE AND amount_unit='yuan' "
                "AND flow_definition IN ('provider_main_net','provider_main_orders_net','main_orders_net') "
                "AND isfinite(main_net) AND fetched_at BETWEEN current_timestamp-INTERVAL 3 HOUR AND current_timestamp",
                [trade_date]).fetchall()]
            reference_meta={'source':'eastmoney_market','receipt_reused':bool(reference_rows)}
            if not reference_rows:
                reference_rows, reference_meta = get_fund_flow_market(
                    trade_date, page_size=500, max_pages=20, pause_seconds=max(float(pause_seconds), 0.5))
            reference_provider = str(reference_meta.get("source") or "eastmoney_market")
            reference_rows = [row for row in reference_rows if row.get("code")]
            if expected_universe:
                reference_rows = [
                    row for row in reference_rows
                    if str(row.get("code") or "") in expected_universe
                ]
            # The reference sweep is validation evidence, not another execution
            # snapshot.  Persisting all ~5,000 reference rows duplicated the
            # market table and caused multi-gigabyte checkpoint pressure.  The
            # durable reconciliation summary below is sufficient for the gate.
            primary_codes = {
                str(row[0]) for row in con.execute(
                    "SELECT DISTINCT stock_code FROM multi_source_stock_flow "
                    "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE AND isfinite(main_net)",
                    [trade_date, source_provider],
                ).fetchall()
            }
            reference_codes = {str(row.get("code")) for row in reference_rows}
            overlap = primary_codes & reference_codes
            overlap_pct = round(len(overlap) * 100.0 / len(reference_codes), 2) if reference_codes else 0.0
            primary_values = {
                str(row[0]): row[1]
                for row in con.execute(
                    "SELECT stock_code, main_net FROM multi_source_stock_flow "
                    "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE AND isfinite(main_net)",
                    [trade_date, source_provider],
                ).fetchall()
            }
            reference_values = {
                str(row.get("code")): row.get("main_net")
                for row in reference_rows
            }
            value_pairs = [
                (primary_values[code], reference_values[code])
                for code in overlap
                if primary_values.get(code) is not None and reference_values.get(code) is not None
            ]
            def _sign(value):
                return 1 if float(value) > 0 else -1 if float(value) < 0 else 0
            sign_disagreement = sum(
                1 for left, right in value_pairs if _sign(left) != _sign(right)
            )
            sign_pct = round(sign_disagreement * 100.0 / len(value_pairs), 2) if value_pairs else None
            mean_abs_diff = (
                round(sum(abs(float(left) - float(right)) for left, right in value_pairs) / len(value_pairs), 2)
                if value_pairs else None
            )
            # Coverage and value agreement are independent dimensions.  A
            # high-overlap sweep with material sign disagreement is a warning,
            # not a silently certified reconciliation.
            value_status = (
                "pass" if value_pairs and (sign_pct or 0.0) <= 5.0
                else "not_observed" if not value_pairs
                else "warning"
            )
            recon_status = (
                "pass" if reference_codes and overlap_pct >= 98.0 and value_status == "pass"
                else "warning"
            )
            reconciliation = {
                "status": recon_status,
                "scope": "matched_native_subset_not_independent_transport",
                "receipt_reused": bool(reference_meta.get('receipt_reused')),
                "reference_provider": reference_provider,
                "reference_rows": len(reference_codes),
                "overlap_rows": len(overlap),
                "primary_only_rows": len(primary_codes - reference_codes),
                "reference_only_rows": len(reference_codes - primary_codes),
                "overlap_reference_pct": overlap_pct,
                "overlap_sign_disagreement": sign_disagreement,
                "overlap_sign_disagreement_pct": sign_pct,
                "mean_abs_main_net_diff": mean_abs_diff,
                "value_status": value_status,
            }
            con.execute(
                "INSERT INTO intraday_stock_flow_reconciliation "
                "(trade_date,primary_provider,primary_rows,reference_provider,reference_rows,overlap_rows,"
                "primary_only_rows,reference_only_rows,overlap_reference_pct,overlap_sign_disagreement,"
                "overlap_sign_disagreement_pct,mean_abs_main_net_diff,value_status,status,last_error,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp) "
                "ON CONFLICT(trade_date) DO UPDATE SET primary_provider=excluded.primary_provider,"
                "primary_rows=excluded.primary_rows,reference_provider=excluded.reference_provider,"
                "reference_rows=excluded.reference_rows,overlap_rows=excluded.overlap_rows,"
                "primary_only_rows=excluded.primary_only_rows,reference_only_rows=excluded.reference_only_rows,"
                "overlap_reference_pct=excluded.overlap_reference_pct,"
                "overlap_sign_disagreement=excluded.overlap_sign_disagreement,"
                "overlap_sign_disagreement_pct=excluded.overlap_sign_disagreement_pct,"
                "mean_abs_main_net_diff=excluded.mean_abs_main_net_diff,"
                "value_status=excluded.value_status,status=excluded.status,last_error=NULL,"
                "updated_at=excluded.updated_at",
                [trade_date, source_provider, fetched_rows, reference_provider, len(reference_codes),
                 len(overlap), len(primary_codes - reference_codes), len(reference_codes - primary_codes),
                 overlap_pct, sign_disagreement, sign_pct, mean_abs_diff, value_status,
                 recon_status, ""],
            )
            con.commit()
        except Exception as exc:
            reconciliation["status"] = "error"
            reconciliation["error"] = str(exc)[:500]
            # A DuckDB FatalException invalidates the connection.  Do not let an
            # optional reconciliation error mask the already committed primary
            # batch or crash the collector while trying to record the error.
            try:
                con.execute(
                    "INSERT INTO intraday_stock_flow_reconciliation "
                    "(trade_date,primary_provider,primary_rows,reference_provider,status,value_status,last_error,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,current_timestamp) ON CONFLICT(trade_date) DO UPDATE SET "
                    "primary_provider=excluded.primary_provider,primary_rows=excluded.primary_rows,"
                    "reference_provider=excluded.reference_provider,status=excluded.status,value_status=excluded.value_status,"
                    "last_error=excluded.last_error,updated_at=excluded.updated_at",
                    [trade_date, source_provider, fetched_rows, "eastmoney_market", "error", "not_observed", str(exc)[:500]],
                )
                con.commit()
            except Exception:
                pass

    # A fatal checkpoint error above invalidates the shared connection; do not
    # let MultiSourceStore's close (which may flush/checkpoint) crash the run
    # after the primary batch is already durable.
    try:
        multi_store.close()
    except Exception:
        pass
    return {
        "trade_date": trade_date, "run_id": run_id, "provider": source_provider, "status": status,
        "expected_rows": expected_rows, "fetched_rows": fetched_rows,
        "expected_pages": expected_pages, "fetched_pages": fetched_pages,
        "coverage_pct": coverage, "unavailable_rows": max(0, expected_rows - fetched_rows),
        "error": error, "reconciliation": reconciliation,
        "missing_codes": missing_codes, "exchange_coverage": exchange_coverage,
    }


def render_report(result: dict) -> str:
    return "\n".join([
        "# Intraday Full-Market Stock Capital Flow",
        "",
        f"- trade_date: `{result['trade_date']}`",
        f"- provider: `{result.get('provider') or 'eastmoney_market'}`",
        f"- status: `{result['status']}`",
        f"- pages: `{result['fetched_pages']}/{result['expected_pages']}`",
        f"- stock coverage: `{result['fetched_rows']}/{result['expected_rows']}` ({result['coverage_pct']}%)",
        f"- source rows without usable flow fields: `{result.get('unavailable_rows', 0)}`",
        f"- exchange coverage: `{result.get('exchange_coverage', {})}`",
        f"- error: `{result.get('error') or ''}`",
        f"- after-close reconciliation: `{result.get('reconciliation', {}).get('status', 'not_run')}` "
        f"({result.get('reconciliation', {}).get('overlap_rows', 0)}/"
        f"{result.get('reconciliation', {}).get('reference_rows', 0)})",
        f"- reconciliation value agreement: `{result.get('reconciliation', {}).get('value_status', 'not_observed')}` "
        f"(sign disagreement={result.get('reconciliation', {}).get('overlap_sign_disagreement_pct', 'n/a')}%, "
        f"mean abs main-net diff={result.get('reconciliation', {}).get('mean_abs_main_net_diff', 'n/a')})",
        "",
        "Historical datacenter rows are never relabelled. For today's session, rows come from Eastmoney's live push2 snapshot and are stamped with the verified local session date.",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and persist full-market intraday stock capital flow.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--pause-seconds", type=float, default=0.35)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--phase", choices=("intraday", "close", "supplemental"), required=True)
    parser.add_argument("--out", default="reports/intraday_stock_flow_latest.md")
    args = parser.parse_args()
    if args.date != date.today().isoformat():
        print(f"date={args.date} status=historical_collection_blocked")
        return 2
    result = collect_market_stock_flow(
        args.db, args.date, page_size=args.page_size,
        pause_seconds=args.pause_seconds, resume=args.resume,
        max_pages=args.max_pages, phase=args.phase,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()), f"report={out}")
    return 0 if str(result["status"]).startswith("success") else 2


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(main())
