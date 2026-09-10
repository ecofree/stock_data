"""批量保存盘中全市场个股资金流，并提供可恢复的逐页检查点。"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import os
from pathlib import Path
import sys
import uuid

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH, TODAY
from schema import init_schema
from trade_system.eastmoney_finance import get_fund_flow_market, get_fund_flow_market_realtime
from trade_system.multi_source_store import MultiSourceStore


def _compact(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _number(value):
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _normalize_page(rows: list[dict], trade_date: str) -> list[dict]:
    target = _compact(trade_date)
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("TRADE_DATE") or "")[:10]
        if target and _compact(raw_date) != target:
            continue
        code = str(row.get("SECURITY_CODE") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        super_in = _number(row.get("SUPERDEAL_INFLOW"))
        super_out = _number(row.get("SUPERDEAL_OUTFLOW"))
        big_in = _number(row.get("BIGDEAL_INFLOW"))
        big_out = _number(row.get("BIGDEAL_OUTFLOW"))
        out.append({
            "code": code,
            "date": raw_date[:10],
            # Eastmoney's PRIME_INFLOW is its main-money net estimate.
            "main_net": _number(row.get("PRIME_INFLOW")),
            "super_net": super_in - super_out if super_in is not None and super_out is not None else None,
            "large_net": big_in - big_out if big_in is not None and big_out is not None else None,
            "mid_net": None,
            "small_net": None,
            "close": _number(row.get("CLOSE_PRICE")),
            "change_pct": _number(row.get("CHANGE_RATE")),
            "turnover": _number(row.get("TURNOVERRATE")),
            "name": row.get("SECURITY_NAME_ABBR") or "",
            "raw": row,
        })
    return list({row["code"]: row for row in out}.values())


def _normalize_realtime_page(rows: list[dict], trade_date: str) -> list[dict]:
    """Normalize Eastmoney push2 ``diff`` rows without trusting a date field."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("f12") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "code": code,
            "date": trade_date,
            "main_net": _number(row.get("f62")),
            "super_net": _number(row.get("f66")),
            "large_net": _number(row.get("f72")),
            "mid_net": _number(row.get("f78")),
            "small_net": _number(row.get("f84")),
            "close": _number(row.get("f2")),
            "change_pct": _number(row.get("f3")),
            "turnover": None,
            "name": row.get("f14") or "",
            "raw": row,
        })
    return list({row["code"]: row for row in out}.values())


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


def _a_share_universe_by_exchange(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Return the current point-in-time A-share universe used as denominator.

    B shares are exchange-specific: Shanghai B shares start with 9 and
    Shenzhen B shares start with 2.  Beijing A shares also start with 9, so a
    prefix-only exclusion incorrectly removes the whole Beijing market.
    """
    try:
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
        return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}
    except Exception:
        return {}


def _a_share_universe(con: duckdb.DuckDBPyConnection) -> set[str]:
    return set(_a_share_universe_by_exchange(con))


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
            "AND main_net IS NOT NULL",
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
                              crosscheck_after_close: bool = True) -> dict:
    run_id = f"em_market_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    multi_store = MultiSourceStore(db_path)
    con = multi_store.con
    init_schema(con)
    _ensure_checkpoint_table(con)
    universe_by_exchange = _a_share_universe_by_exchange(con)
    expected_universe = set(universe_by_exchange)
    previous_batch = con.execute(
        "SELECT expected_rows,expected_pages,fetched_rows,fetched_pages,provider,coverage_pct "
        "FROM intraday_stock_flow_batch WHERE trade_date=?",
        [trade_date],
    ).fetchone()
    source_provider = "eastmoney_intraday_clist" if trade_date == date.today().isoformat() else "eastmoney_market"
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
                and prior_coverage >= 99.5
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
        rows = (_normalize_realtime_page(raw_rows, trade_date)
                if source_provider in {"eastmoney_intraday_clist", "eastmoney_intraday_clist_delay"}
                else _normalize_page(raw_rows, trade_date))
        # The live clist contains B shares and can repeat rows at page
        # boundaries while its sort order moves.  The project contract is the
        # canonical A-share universe; filter before persistence and suppress
        # cross-page duplicates so the unique business key remains meaningful.
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
             "page_no": page_no, "pages": pages},
            asset_type="stock", trade_date=trade_date, commit=not atomic_refresh,
        )
        con.execute(
            "INSERT INTO intraday_stock_flow_page_checkpoint(trade_date,page_no,pages_expected,status,rows_written,last_error,updated_at) "
            "VALUES (?,?,?,?,?,?,current_timestamp) ON CONFLICT(trade_date,page_no) DO UPDATE SET pages_expected=excluded.pages_expected,status=excluded.status,rows_written=excluded.rows_written,last_error=excluded.last_error,updated_at=excluded.updated_at",
            [trade_date, page_no, pages, "success" if stored.get("rows_written", 0) else "empty",
             int(stored.get("rows_written", 0)), ""],
        )
        if not atomic_refresh:
            con.commit()
        written_pages.add(page_no)

    try:
        if atomic_refresh:
            con.execute("BEGIN TRANSACTION")
        if completed_resume:
            rows, meta = [], {
                "source": source_provider,
                "pages": int(previous_batch[1] or 0),
                "expected_rows": int(previous_batch[0] or 0),
                "rows": int(previous_batch[2] or 0),
                "status": "existing_complete_pages",
            }
        elif source_provider in {"eastmoney_intraday_clist", "eastmoney_intraday_clist_delay"}:
            # During the session the datacenter/report endpoint is commonly
            # one session behind.  Use Eastmoney's live clist route directly
            # for today's snapshot; the historical route remains available
            # for backfills and unit-testable historical runs.
            rows, meta = get_fund_flow_market_realtime(
                trade_date, page_size=min(page_size, 100), max_pages=max_pages,
                pause_seconds=pause_seconds, on_page=on_page,
                start_page=resume_start_page,
            )
        else:
            rows, meta = get_fund_flow_market(
                trade_date, page_size=page_size, max_pages=max_pages,
                pause_seconds=pause_seconds, on_page=on_page,
            )
        expected_pages = int(meta.get("pages") or expected_pages or 0)
        expected_rows = len(expected_universe) or int(meta.get("expected_rows") or expected_rows or 0)
        source_provider = str(meta.get("source") or source_provider)
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
            "SELECT count(DISTINCT stock_code) FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE",
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
                "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE",
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
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        error = str(exc)[:500]
        fetched_rows = int(con.execute(
            "SELECT count(DISTINCT stock_code) FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE",
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
                "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE",
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
            and crosscheck_after_close and is_after_close and max_pages is None):
        try:
            reference_rows, reference_meta = get_fund_flow_market(
                trade_date, page_size=500, pause_seconds=max(float(pause_seconds), 0.5),
            )
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
                    "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE",
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
                    "WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE",
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
    parser.add_argument("--out", default="reports/intraday_stock_flow_latest.md")
    args = parser.parse_args()
    if args.date != date.today().isoformat():
        print(f"date={args.date} status=historical_collection_blocked")
        return 2
    result = collect_market_stock_flow(
        args.db, args.date, page_size=args.page_size,
        pause_seconds=args.pause_seconds, resume=args.resume,
        max_pages=args.max_pages,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()), f"report={out}")
    return 0 if str(result["status"]).startswith("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
