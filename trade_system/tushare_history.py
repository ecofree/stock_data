"""Batch, resumable TuShare history collection for calendar-year analysis.

The relay accepts date-wide ``daily``/``daily_basic`` requests.  ``moneyflow``
is paged at 1,000 rows, then normalized into the project's source-aware flow
tables.  Every date/dataset is committed before the next request starts.
"""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import time
from typing import Any, Iterable

import duckdb

from base import DuckDBStore
from schema import init_schema
from trade_system.tushare_relay import (
    TushareRelayClient,
    TushareRelayError,
    collect_tushare_stock_basic,
    collect_tushare_trade_cal,
    ts_code_to_stock_code,
)
from trade_system.flow_contract import ensure_stock_flow_contract


CHECKPOINT_DATE = "1900-01-01"
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
DAILY_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv"
ADJ_FACTOR_FIELDS = "ts_code,trade_date,adj_factor"
# The relay is more reliable when large field lists are split into two
# requests.  In particular, an all-field ``moneyflow`` request can fail while
# either of these smaller projections succeeds.  The collector merges them by
# the natural key before writing the raw staging table.
MONEYFLOW_MAIN_FIELDS = (
    "ts_code,trade_date,buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount,net_mf_amount"
)
MONEYFLOW_SIZE_FIELDS = (
    "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
    "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount"
)
INDUSTRY_MAIN_FIELDS = (
    "trade_date,ts_code,name,pct_change,close,net_amount,buy_elg_amount,sell_elg_amount,"
    "buy_lg_amount,sell_lg_amount"
)
INDUSTRY_SIZE_FIELDS = "trade_date,ts_code,buy_md_amount,sell_md_amount,buy_sm_amount,sell_sm_amount"
# The migrated relay accepts up to roughly 1,000 comma-separated codes for
# the main moneyflow projection.  Keeping the fallback at this size reduces
# a full-market day from ~13 requests to ~5 without widening the rejected
# size-bucket projection.
MONEYFLOW_CODE_BATCH_SIZE = 1000


def _iso(value: str | date) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    raise ValueError(f"invalid date: {value}")


def _ymd(value: str | date) -> str:
    return _iso(value).replace("-", "")


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _pages(client: TushareRelayClient, api: str, params: dict[str, Any], fields: str,
           *, page_size: int, max_pages: int = 10) -> Iterable[tuple[int, list[dict[str, Any]]]]:
    for page_no in range(max_pages):
        query = dict(params)
        query.update(limit=page_size, offset=page_no * page_size)
        rows = client.query_rows(api, query, fields)
        yield page_no, rows
        if len(rows) < page_size:
            break


class TushareHistoryCollector:
    def __init__(self, db_path: str | Path, *, client: TushareRelayClient | None = None,
                 request_timeout: int = 20, retries: int = 3,
                 batch_limit: int = 5000, moneyflow_page_size: int = 1000,
                 budget_seconds: float = 300.0):
        self.db_path = str(db_path)
        self.store = DuckDBStore(self.db_path)
        init_schema(self.store.conn)
        ensure_stock_flow_contract(self.store.conn)
        self.client = client or TushareRelayClient(timeout=request_timeout, retries=retries)
        self.batch_limit = max(100, int(batch_limit))
        self.moneyflow_page_size = max(100, min(int(moneyflow_page_size), 1000))
        self.budget_seconds = max(1.0, float(budget_seconds))
        self.started = time.monotonic()

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "TushareHistoryCollector":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _budget_left(self) -> bool:
        return time.monotonic() - self.started < self.budget_seconds

    def _checkpoint(self, dataset: str, trade_date: str, status: str, *, rows: int = 0,
                    attempts: int = 0, error: str = "") -> None:
        now = datetime.now()
        self.store.conn.execute(
            "INSERT INTO history_fetch_checkpoint "
            "(dataset,trade_date,page_no,status,rows_written,attempts,last_error,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (dataset,trade_date,page_no) DO UPDATE SET status=excluded.status,"
            "rows_written=excluded.rows_written,attempts=excluded.attempts,last_error=excluded.last_error,"
            "updated_at=excluded.updated_at",
            [dataset, _iso(trade_date), 0, status, rows, attempts, error[:500], now],
        )
        self.store.conn.commit()

    def _is_done(self, dataset: str, trade_date: str, force: bool) -> bool:
        if force:
            return False
        row = self.store.conn.execute(
            "SELECT status FROM history_fetch_checkpoint WHERE dataset=? AND trade_date=? AND page_no=0",
            [dataset, _iso(trade_date)],
        ).fetchone()
        if not row or row[0] != "success":
            return False
        # A previous relay version silently truncated date-wide responses at
        # exactly 5,000 rows.  Such a checkpoint is not complete even though
        # the old collector recorded HTTP success.  Re-fetch it automatically
        # so normal resumable runs repair the historical data without needing
        # a destructive global --force.
        table_by_dataset = {
            "daily": ("tushare_daily", "date"),
            "daily_basic": ("tushare_daily_basic", "date"),
            "adj_factor": ("tushare_adj_factor", "date"),
            "moneyflow": ("tushare_moneyflow", "date"),
            "industry_flow": ("tushare_moneyflow_industry", "trade_date"),
        }
        if dataset == "stock_basic":
            count = int(self.store.conn.execute("SELECT count(*) FROM tushare_stock_basic").fetchone()[0] or 0)
            return count > 0 and count != 5000
        table_info = table_by_dataset.get(dataset)
        if table_info:
            table, date_column = table_info
            count = int(self.store.conn.execute(
                f"SELECT count(*) FROM {table} WHERE {date_column}=?", [_iso(trade_date)]
            ).fetchone()[0] or 0)
            if count == 5000:
                return False
            # A success checkpoint with zero stored rows means an empty relay
            # response was recorded as success (observed 2026-08-10 on
            # daily/daily_basic).  Treat it as not-done so the next run
            # re-fetches the date instead of permanently skipping it.
            if count == 0:
                return False
        return True

    def _next_attempt(self, dataset: str, trade_date: str) -> int:
        row = self.store.conn.execute(
            "SELECT attempts FROM history_fetch_checkpoint "
            "WHERE dataset=? AND trade_date=? AND page_no=0",
            [dataset, _iso(trade_date)],
        ).fetchone()
        return int((row[0] if row else 0) or 0) + 1

    def _bulk_replace(self, table: str, rows: list[tuple], columns: list[str], replace_on: list[str]) -> int:
        """Write a batch with one temp-table load and set-based delete/insert."""
        if not rows:
            return 0
        temp = "_history_batch"
        self.store.conn.execute(f"DROP TABLE IF EXISTS {temp}")
        projection = ",".join(columns)
        self.store.conn.execute(f"CREATE TEMP TABLE {temp} AS SELECT {projection} FROM {table} LIMIT 0")
        placeholders = ",".join("?" for _ in columns)
        self.store.conn.executemany(f"INSERT INTO {temp}({projection}) VALUES ({placeholders})", rows)
        join = " AND ".join(f"target.{key}=batch.{key}" for key in replace_on)
        self.store.conn.execute(
            f"DELETE FROM {table} AS target WHERE EXISTS (SELECT 1 FROM {temp} AS batch WHERE {join})"
        )
        self.store.conn.execute(f"INSERT INTO {table}({projection}) SELECT {projection} FROM {temp}")
        self.store.conn.execute(f"DROP TABLE {temp}")
        return len(rows)

    def _query_date_batch(self, api: str, trade_date: str, fields: str, *, with_limit: bool = True) -> list[dict[str, Any]]:
        """Fetch a date-wide batch and reject an implausibly short relay response.

        A transient relay response containing only a few dozen stocks is a
        successful HTTP response but not a complete market snapshot.  Fake
        clients used by tests are intentionally exempt from this production
        guard.
        """
        params = {"trade_date": _ymd(trade_date)}
        if with_limit:
            params["limit"] = self.batch_limit
        rows = self.client.query_rows(api, params, fields)
        target = _iso(trade_date)
        dated_rows = []
        for row in rows:
            try:
                if row.get("trade_date") and _iso(row.get("trade_date")) == target:
                    dated_rows.append(row)
            except (TypeError, ValueError):
                continue
        if isinstance(self.client, TushareRelayClient) and rows and not dated_rows:
            raise TushareRelayError(f"{api} returned no rows for requested date {target}")
        # Never relabel a previous-session response as the requested date.
        rows = dated_rows
        # A relay that answers with an empty item list is a successful HTTP
        # response, not a market snapshot.  Without this guard an empty day
        # is recorded as a success checkpoint after DELETE+INSERT of zero
        # rows (observed 2026-08-10: daily/daily_basic marked success with 0
        # rows while adj_factor for the same date landed 5,553 rows).
        if isinstance(self.client, TushareRelayClient) and not rows:
            raise TushareRelayError(f"empty {api} response for requested date {target}")
        if isinstance(self.client, TushareRelayClient) and 0 < len(rows) < 1000:
            time.sleep(1)
            retry = self.client.query_rows(api, params, fields)
            if len(retry) > len(rows):
                rows = retry
            if 0 < len(rows) < 1000:
                raise TushareRelayError(f"suspiciously short {api} response: {len(rows)} rows")
        return rows

    def ensure_calendar(self, start_date: str, end_date: str) -> list[str]:
        start = datetime.strptime(_iso(start_date), "%Y-%m-%d").date()
        end = datetime.strptime(_iso(end_date), "%Y-%m-%d").date()
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        expected_calendar_days = (end - start).days + 1
        params = [_iso(start_date), _iso(end_date)]
        stored_calendar_days = int(self.store.conn.execute(
            "SELECT count(DISTINCT cal_date) FROM tushare_trade_cal "
            "WHERE cal_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)",
            params,
        ).fetchone()[0])
        if stored_calendar_days < expected_calendar_days:
            try:
                collect_tushare_trade_cal(
                    self.client, self.store, _ymd(start_date), _ymd(end_date)
                )
                self.store.conn.commit()
            except Exception as exc:
                raise TushareRelayError(
                    f"trade_cal fetch failed for {_iso(start_date)}..{_iso(end_date)}: {exc}"
                ) from exc
            stored_calendar_days = int(self.store.conn.execute(
                "SELECT count(DISTINCT cal_date) FROM tushare_trade_cal "
                "WHERE cal_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)",
                params,
            ).fetchone()[0])
        if stored_calendar_days < expected_calendar_days:
            raise TushareRelayError(
                "trade_cal incomplete for "
                f"{_iso(start_date)}..{_iso(end_date)}: "
                f"{stored_calendar_days}/{expected_calendar_days} calendar days"
            )
        rows = self.store.conn.execute(
            "SELECT CAST(cal_date AS VARCHAR) FROM tushare_trade_cal "
            "WHERE cal_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) AND is_open=TRUE ORDER BY cal_date",
            params,
        ).fetchall()
        # An empty result is valid only after the entire calendar range has
        # been verified (for example a weekend or exchange holiday).  Never
        # synthesize weekdays when the upstream calendar is absent.
        return [str(row[0])[:10] for row in rows]

    def collect_stock_basic(self, *, force: bool = False) -> int:
        if self._is_done("stock_basic", CHECKPOINT_DATE, force):
            return 0
        self._checkpoint("stock_basic", CHECKPOINT_DATE, "running", attempts=1)
        try:
            rows = collect_tushare_stock_basic(self.client, self.store)
            self.store.conn.commit()
            self._checkpoint("stock_basic", CHECKPOINT_DATE, "success", rows=rows, attempts=1)
            return rows
        except Exception as exc:
            self._checkpoint("stock_basic", CHECKPOINT_DATE, "error", attempts=1, error=str(exc))
            return 0

    def _collect_daily(self, trade_date: str) -> int:
        # Date-wide daily responses fit in the relay's 5,000-row envelope for
        # the current listed universe.  Avoid a second offset request: some
        # relay deployments reject an offset exactly at the first page size.
        # The migrated relay returns the complete date snapshot when no limit
        # is supplied (recent listed universe is >5,000 rows).  Supplying the
        # nominal 5,000-row limit silently truncates the tail and makes a
        # partial day look successful.
        rows = self._query_date_batch("daily", trade_date, DAILY_FIELDS, with_limit=False)
        deduped = {}
        for row in rows:
            if row.get("ts_code") and row.get("trade_date"):
                deduped[(row.get("ts_code"), _iso(row.get("trade_date")))] = (
                    row.get("ts_code"), ts_code_to_stock_code(row.get("ts_code")), _iso(row.get("trade_date")),
                    _num(row.get("open")), _num(row.get("high")), _num(row.get("low")), _num(row.get("close")),
                    _num(row.get("vol")), _num(row.get("amount")), _num(row.get("pct_chg")))
        out = list(deduped.values())
        # Clear stale/partial rows only after a complete-looking response has
        # arrived, so a failed request never destroys the last usable batch.
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_daily WHERE date=?", [_iso(trade_date)])
            count = self._bulk_replace(
                "tushare_daily", out,
                ["ts_code", "stock_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
                ["ts_code", "date"],
            )
            self.store.conn.execute("COMMIT")
            return count
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def _collect_daily_basic(self, trade_date: str) -> int:
        rows = self._query_date_batch("daily_basic", trade_date, DAILY_BASIC_FIELDS, with_limit=False)
        deduped = {}
        for row in rows:
            if row.get("ts_code") and row.get("trade_date"):
                deduped[(row.get("ts_code"), _iso(row.get("trade_date")))] = (
                    row.get("ts_code"), ts_code_to_stock_code(row.get("ts_code")), _iso(row.get("trade_date")),
                    _num(row.get("turnover_rate")), _num(row.get("volume_ratio")), _num(row.get("pe")),
                    _num(row.get("pb")), _num(row.get("total_mv")), _num(row.get("circ_mv")))
        out = list(deduped.values())
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_daily_basic WHERE date=?", [_iso(trade_date)])
            count = self._bulk_replace(
                "tushare_daily_basic", out,
                ["ts_code", "stock_code", "date", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv"],
                ["ts_code", "date"],
            )
            self.store.conn.execute("COMMIT")
            return count
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def _collect_adj_factor(self, trade_date: str) -> int:
        # Like daily/daily_basic, the relay's date-wide response is the
        # complete market snapshot only when no 5,000-row limit is supplied.
        rows = self._query_date_batch("adj_factor", trade_date, ADJ_FACTOR_FIELDS, with_limit=False)
        deduped = {}
        for row in rows:
            if row.get("ts_code") and row.get("trade_date") and row.get("adj_factor") is not None:
                deduped[(row.get("ts_code"), _iso(row.get("trade_date")))] = (
                    row.get("ts_code"), ts_code_to_stock_code(row.get("ts_code")),
                    _iso(row.get("trade_date")), _num(row.get("adj_factor")))
        out = list(deduped.values())
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_adj_factor WHERE date=?", [_iso(trade_date)])
            count = self._bulk_replace(
                "tushare_adj_factor", out,
                ["ts_code", "stock_code", "date", "adj_factor"],
                ["ts_code", "date"],
            )
            self.store.conn.execute("COMMIT")
            return count
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def _collect_moneyflow(self, trade_date: str) -> int:
        total = 0
        # Fetch/publish as one transaction.  A failed relay page must not erase
        # the previous verified market snapshot.
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            # The fast relay returns the complete market snapshot when the
            # date filter is sent without ``limit``/``offset``.  Its paged
            # form can return an empty first page or an incomplete tail, so
            # prefer the bounded, date-wide response and only use the older
            # paging/code-batch path as a fallback.
            if isinstance(self.client, TushareRelayClient):
                full_rows = self.client.query_rows(
                    "moneyflow", {"trade_date": _ymd(trade_date)}, MONEYFLOW_MAIN_FIELDS
                )
                if len(full_rows) >= 4000:
                    details: list[dict[str, Any]] = []
                    try:
                        details = self.client.query_rows(
                            "moneyflow", {"trade_date": _ymd(trade_date)}, MONEYFLOW_SIZE_FIELDS
                        )
                    except Exception:
                        details = []
                    detail_by_key = {
                        (str(row.get("ts_code")), _iso(row.get("trade_date"))): row
                        for row in details
                    }
                    merged_rows = []
                    for row in full_rows:
                        key = (str(row.get("ts_code")), _iso(row.get("trade_date")))
                        merged = dict(detail_by_key.get(key) or {})
                        merged.update(row)
                        merged_rows.append(merged)
                    out = [
                        (row.get("ts_code"), ts_code_to_stock_code(row.get("ts_code")), _iso(row.get("trade_date")),
                         _num(row.get("buy_sm_amount")), _num(row.get("sell_sm_amount")),
                         _num(row.get("buy_md_amount")), _num(row.get("sell_md_amount")),
                         _num(row.get("buy_lg_amount")), _num(row.get("sell_lg_amount")),
                         _num(row.get("buy_elg_amount")), _num(row.get("sell_elg_amount")),
                         _num(row.get("net_mf_amount")))
                        for row in merged_rows if row.get("ts_code") and row.get("trade_date")
                    ]
                    total = self._bulk_replace(
                        "tushare_moneyflow", out,
                        ["ts_code", "stock_code", "date", "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
                         "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_amount"],
                        ["ts_code", "date"],
                    )
                    self.store.conn.execute("COMMIT")
                    return total
            for page_no in range(10):
                params = {"trade_date": _ymd(trade_date), "limit": self.moneyflow_page_size,
                          "offset": page_no * self.moneyflow_page_size}
                # Main/net amounts are the required projection.  Size buckets
                # are best-effort because the relay may reject the larger
                # projection; missing size values remain NULL instead of
                # losing net flow.
                rows = self.client.query_rows("moneyflow", params, MONEYFLOW_MAIN_FIELDS)
                if not rows:
                    break
                detail_by_key: dict[tuple[str, str], dict[str, Any]] = {}
                try:
                    details = self.client.query_rows("moneyflow", params, MONEYFLOW_SIZE_FIELDS)
                    detail_by_key = {(str(r.get("ts_code")), _iso(r.get("trade_date"))): r for r in details}
                except Exception:
                    detail_by_key = {}
                merged = []
                for row in rows:
                    key = (str(row.get("ts_code")), _iso(row.get("trade_date")))
                    merged_row = dict(detail_by_key.get(key) or {})
                    merged_row.update(row)
                    merged.append(merged_row)
                out = [
                    (row.get("ts_code"), ts_code_to_stock_code(row.get("ts_code")), _iso(row.get("trade_date")),
                     _num(row.get("buy_sm_amount")), _num(row.get("sell_sm_amount")),
                     _num(row.get("buy_md_amount")), _num(row.get("sell_md_amount")),
                     _num(row.get("buy_lg_amount")), _num(row.get("sell_lg_amount")),
                     _num(row.get("buy_elg_amount")), _num(row.get("sell_elg_amount")),
                     _num(row.get("net_mf_amount")))
                    for row in merged if row.get("ts_code") and row.get("trade_date")
                ]
                total += self._bulk_replace(
                    "tushare_moneyflow", out,
                    ["ts_code", "stock_code", "date", "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
                     "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_amount"],
                    ["ts_code", "date"],
                )
                if len(rows) < self.moneyflow_page_size:
                    break
            if isinstance(self.client, TushareRelayClient) and total < 4000:
                # Some relay deployments index recent moneyflow rows by
                # ``ts_code`` but return an empty/short result for the same
                # request filtered only by ``trade_date``.  Retry the date in
                # bounded comma-separated code batches before declaring a
                # missing day.  The main/net projection is intentionally
                # used here: the relay rejects the wider size-bucket
                # projection for large code lists, while net/main values are
                # sufficient for the normalized capital-flow tables.
                codes = [row[0] for row in self.store.conn.execute(
                    "SELECT DISTINCT ts_code FROM tushare_stock_basic "
                    "WHERE ts_code IS NOT NULL ORDER BY ts_code"
                ).fetchall()]
                fallback_rows: list[dict[str, Any]] = []
                def query_code_batch(code_batch: list[str], depth: int = 0) -> list[dict[str, Any]]:
                    if not code_batch or not self._budget_left():
                        return []
                    best_batch: list[dict[str, Any]] = []
                    for attempt in range(2):
                        try:
                            candidate = self.client.query_rows(
                                "moneyflow",
                                {"ts_code": ",".join(code_batch), "trade_date": _ymd(trade_date), "limit": 2000},
                                MONEYFLOW_MAIN_FIELDS,
                            )
                            if len(candidate) > len(best_batch):
                                best_batch = candidate
                            if len(candidate) >= max(1, int(len(code_batch) * 0.75)):
                                return candidate
                        except Exception:
                            if attempt:
                                break
                    # A transiently short 1,000-code response can often be
                    # recovered by splitting the offending slice.  Stop at
                    # 250 codes so the relay URL remains bounded.
                    if depth < 2 and len(code_batch) > 250 and self._budget_left():
                        midpoint = len(code_batch) // 2
                        return query_code_batch(code_batch[:midpoint], depth + 1) + query_code_batch(code_batch[midpoint:], depth + 1)
                    return best_batch

                for start in range(0, len(codes), MONEYFLOW_CODE_BATCH_SIZE):
                    fallback_rows.extend(query_code_batch(codes[start:start + MONEYFLOW_CODE_BATCH_SIZE]))
                deduped = {}
                for row in fallback_rows:
                    if row.get("ts_code") and row.get("trade_date"):
                        deduped[(str(row.get("ts_code")), _iso(row.get("trade_date")))] = row
                out = [
                    (row.get("ts_code"), ts_code_to_stock_code(row.get("ts_code")), _iso(row.get("trade_date")),
                     _num(row.get("buy_sm_amount")), _num(row.get("sell_sm_amount")),
                     _num(row.get("buy_md_amount")), _num(row.get("sell_md_amount")),
                     _num(row.get("buy_lg_amount")), _num(row.get("sell_lg_amount")),
                     _num(row.get("buy_elg_amount")), _num(row.get("sell_elg_amount")),
                     _num(row.get("net_mf_amount")))
                    for row in deduped.values()
                ]
                self.store.conn.execute("DELETE FROM tushare_moneyflow WHERE date=?", [_iso(trade_date)])
                total = self._bulk_replace(
                    "tushare_moneyflow", out,
                    ["ts_code", "stock_code", "date", "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
                     "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_amount"],
                    ["ts_code", "date"],
                )
                if total < 4000:
                    raise TushareRelayError(f"incomplete moneyflow response: {total} rows")
            self.store.conn.execute("COMMIT")
            return total
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def _collect_industry_flow(self, trade_date: str) -> int:
        last_error = None
        rows: list[dict[str, Any]] = []
        for api in ("moneyflow_ind_dc", "moneyflow_ind_ths"):
            try:
                params = {"trade_date": _ymd(trade_date)}
                try:
                    # Industry endpoints commonly return the full daily set
                    # (about 1,000 rows) without pagination.  This avoids the
                    # relay's unreliable offset-at-page-boundary behaviour.
                    core = self.client.query_rows(api, params, INDUSTRY_MAIN_FIELDS)
                except Exception:
                    core = self.client.query_rows(
                        api, {**params, "limit": 1000, "offset": 0}, INDUSTRY_MAIN_FIELDS
                    )
                details: list[dict[str, Any]] = []
                try:
                    details = self.client.query_rows(api, params, INDUSTRY_SIZE_FIELDS)
                except Exception:
                    details = []
                detail_by_key = {(str(r.get("ts_code")), _iso(r.get("trade_date"))): r for r in details}
                rows = []
                for row in core:
                    key = (str(row.get("ts_code")), _iso(row.get("trade_date")))
                    merged = dict(detail_by_key.get(key) or {})
                    merged.update(row)
                    rows.append(merged)
                if rows and (not isinstance(self.client, TushareRelayClient) or len(rows) >= 500):
                    break
            except Exception as exc:
                last_error = exc
        if not rows and last_error:
            raise last_error
        if isinstance(self.client, TushareRelayClient) and not rows:
            raise TushareRelayError("empty industry flow response")
        if isinstance(self.client, TushareRelayClient) and 0 < len(rows) < 500:
            raise TushareRelayError(f"incomplete industry flow response: {len(rows)} rows")
        out = [
            (_iso(row.get("trade_date") or trade_date), row.get("ts_code"), row.get("name"), _num(row.get("pct_change")),
             _num(row.get("close")), _num(row.get("net_amount")), _num(row.get("buy_elg_amount")),
             _num(row.get("sell_elg_amount")), _num(row.get("buy_lg_amount")), _num(row.get("sell_lg_amount")),
             _num(row.get("buy_md_amount")), _num(row.get("sell_md_amount")), _num(row.get("buy_sm_amount")),
             _num(row.get("sell_sm_amount")), _json(row))
            for row in rows if row.get("ts_code")
        ]
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_moneyflow_industry WHERE trade_date=?", [_iso(trade_date)])
            count = self._bulk_replace(
                "tushare_moneyflow_industry", out,
                ["trade_date", "ts_code", "sector_name", "change_pct", "close", "net_amount", "buy_elg_amount", "sell_elg_amount",
                 "buy_lg_amount", "sell_lg_amount", "buy_md_amount", "sell_md_amount", "buy_sm_amount", "sell_sm_amount", "raw_json"],
                ["trade_date", "ts_code"],
            )
            self.store.conn.execute("COMMIT")
            return count
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def sync_stock_flow(self, trade_date: str) -> int:
        rows = self.store.conn.execute(
            "SELECT ts_code,stock_code,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
            "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount "
            "FROM tushare_moneyflow WHERE date=? ORDER BY fetched_at DESC",
            [_iso(trade_date)],
        ).fetchall()
        out = []
        seen = set()
        for row in rows:
            if row[1] in seen:
                continue
            seen.add(row[1])
            small = ((_num(row[2]) or 0) - (_num(row[3]) or 0)) * 10000
            mid = ((_num(row[4]) or 0) - (_num(row[5]) or 0)) * 10000
            large = ((_num(row[6]) or 0) - (_num(row[7]) or 0)) * 10000
            super_net = ((_num(row[8]) or 0) - (_num(row[9]) or 0)) * 10000
            total = (_num(row[10]) * 10000) if row[10] is not None else small + mid + large + super_net
            main = super_net + large
            out.append([_iso(trade_date), row[1], main, total, super_net, large, mid, small, "tushare",
                        "yuan", "main_orders_net", "moneyflow", "tushare", "stock_flow_v2", False,
                        _json({"ts_code": row[0], "source": "tushare_moneyflow", "unit": "yuan",
                               "net_total": total, "main_net_definition": "super_net+large_net"})])
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute(
                "DELETE FROM multi_source_stock_flow WHERE source_date=? AND provider='tushare'",
                [_iso(trade_date)],
            )
            count = self._bulk_replace(
                "multi_source_stock_flow", out,
                ["source_date", "stock_code", "main_net", "net_total", "super_net", "large_net", "mid_net", "small_net", "provider", "amount_unit", "flow_definition", "source_api", "origin_provider", "field_mapping_version", "is_stale", "raw_json"],
                ["source_date", "stock_code", "provider"],
            )
            self.store.conn.execute("COMMIT")
            return count
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def sync_sector_flow(self, trade_date: str) -> int:
        rows = self.store.conn.execute(
            "SELECT ts_code,sector_name,change_pct,close,net_amount,buy_elg_amount,sell_elg_amount,"
            "buy_lg_amount,sell_lg_amount,buy_md_amount,sell_md_amount,buy_sm_amount,sell_sm_amount "
            "FROM tushare_moneyflow_industry WHERE trade_date=? AND close IS NOT NULL AND close>0",
            [_iso(trade_date)],
        ).fetchall()
        out = []
        for row in rows:
            # Unlike stock-level ``moneyflow`` (万元), TuShare's DC industry
            # endpoint documents these fields as yuan.  Do not multiply them
            # again or sector totals become four orders of magnitude too high.
            super_net = (_num(row[5]) or 0) - (_num(row[6]) or 0)
            large = (_num(row[7]) or 0) - (_num(row[8]) or 0)
            mid = (_num(row[9]) or 0) - (_num(row[10]) or 0)
            small = (_num(row[11]) or 0) - (_num(row[12]) or 0)
            out.append([_iso(trade_date), row[0], row[1], _num(row[4]) if row[4] is not None else None,
                        super_net, large, mid, small, row[2], "tushare_sector_full",
                        "tushare_dc_sector", "yuan", False,
                        _json({"close": row[3], "source": "moneyflow_ind_dc", "unit": "yuan"})])
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute(
                "DELETE FROM multi_source_sector_flow WHERE source_date=? AND provider IN ('tushare','tushare_sector_full')",
                [_iso(trade_date)],
            )
            count = self._bulk_replace(
                "multi_source_sector_flow", out,
                ["source_date", "sector_code", "sector_name", "main_net", "super_net", "large_net", "mid_net", "small_net", "change_pct", "provider", "sector_type", "amount_unit", "is_stale", "raw_json"],
                ["source_date", "sector_code", "provider"],
            )
            self.store.conn.execute("COMMIT")
            return count
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def run(self, start_date: str, end_date: str, *, datasets: Iterable[str],
            max_days: int | None = None, force: bool = False,
            retry_passes: int = 0, retry_delay_seconds: float = 0.0) -> dict[str, Any]:
        datasets = list(dict.fromkeys(datasets))
        dates = self.ensure_calendar(start_date, end_date)
        if max_days is not None:
            dates = dates[: max(0, int(max_days))]
        results_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        if "stock_basic" in datasets and self._budget_left():
            results_by_key[("stock_basic", CHECKPOINT_DATE)] = {
                "dataset": "stock_basic",
                "trade_date": CHECKPOINT_DATE,
                "status": "success" if self.collect_stock_basic(force=force) else "empty",
            }
        handlers = {
            "daily": self._collect_daily,
            "daily_basic": self._collect_daily_basic,
            "adj_factor": self._collect_adj_factor,
            "moneyflow": self._collect_moneyflow,
            "industry_flow": self._collect_industry_flow,
        }
        retry_keys: set[tuple[str, str]] | None = None
        for pass_no in range(max(0, int(retry_passes)) + 1):
            failed_keys: set[tuple[str, str]] = set()
            for trade_date in dates:
                for dataset in datasets:
                    if dataset == "stock_basic" or dataset not in handlers:
                        continue
                    key = (dataset, _iso(trade_date))
                    if retry_keys is not None and key not in retry_keys:
                        continue
                    if not self._budget_left():
                        results_by_key[key] = {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "budget_exhausted",
                        }
                        continue
                    if self._is_done(dataset, trade_date, force):
                        results_by_key.setdefault(key, {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "skipped",
                        })
                        continue
                    attempt = self._next_attempt(dataset, trade_date)
                    self._checkpoint(dataset, trade_date, "running", attempts=attempt)
                    try:
                        rows = handlers[dataset](trade_date)
                        if dataset == "moneyflow":
                            rows = self.sync_stock_flow(trade_date)
                        elif dataset == "industry_flow":
                            rows = self.sync_sector_flow(trade_date)
                        self._checkpoint(dataset, trade_date, "success", rows=rows, attempts=attempt)
                        results_by_key[key] = {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "success",
                            "rows": rows,
                        }
                    except Exception as exc:
                        # The handlers publish inside a transaction.  Preserve the
                        # last verified date on a network/validation failure and
                        # make the checkpoint carry the error instead of deleting
                        # good data from the production tables.
                        self._checkpoint(dataset, trade_date, "error", attempts=attempt, error=str(exc))
                        results_by_key[key] = {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "error",
                            "error": str(exc)[:240],
                        }
                        failed_keys.add(key)
            if not failed_keys or pass_no >= max(0, int(retry_passes)):
                break
            retry_keys = failed_keys
            delay = max(0.0, float(retry_delay_seconds)) * (pass_no + 1)
            remaining = self.budget_seconds - (time.monotonic() - self.started)
            if remaining <= 1.0:
                break
            if delay:
                time.sleep(min(delay, max(0.0, remaining - 1.0)))
        return {"start_date": _iso(start_date), "end_date": _iso(end_date), "dates": dates,
                "datasets": datasets, "results": list(results_by_key.values()),
                "elapsed_seconds": round(time.monotonic() - self.started, 3)}


def render_report(db_path: str | Path, result: dict[str, Any], out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = con.execute(
            "SELECT dataset,status,count(*),sum(rows_written),max(updated_at) FROM history_fetch_checkpoint "
            "GROUP BY dataset,status ORDER BY dataset,status"
        ).fetchall()
        tables = []
        table_date_expr = {
            "tushare_stock_basic": "max(list_date)",
            "tushare_daily": "max(date)",
            "tushare_daily_basic": "max(date)",
            "tushare_adj_factor": "max(date)",
            "tushare_moneyflow": "max(date)",
            "tushare_moneyflow_industry": "max(trade_date)",
            "multi_source_stock_flow": "max(source_date)",
            "multi_source_sector_flow": "max(source_date)",
        }
        for table, date_expr in table_date_expr.items():
            try:
                tables.append((table, *con.execute(f"SELECT count(*),{date_expr} FROM {table}").fetchone()))
            except Exception:
                pass
    finally:
        con.close()
    lines = ["# Tushare 2026 历史回填", "", f"- 日期范围: `{result['start_date']}` ~ `{result['end_date']}`",
             f"- 本次交易日数: {len(result['dates'])}", f"- elapsed_seconds: {result['elapsed_seconds']}", "",
             "## 检查点", "", "| dataset | status | dates/tasks | rows | last_updated |", "|---|---|---:|---:|"]
    lines.extend(f"| {row[0]} | {row[1]} | {row[2]} | {row[3] or 0} | {row[4] or '-'} |" for row in counts)
    lines.extend(["", "## 表覆盖", "", "| table | rows | latest_date |", "|---|---:|---|"])
    lines.extend(f"| {table} | {rows} | {latest or '-'} |" for table, rows, latest in tables)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
