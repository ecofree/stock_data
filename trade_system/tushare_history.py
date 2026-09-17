"""Batch, resumable TuShare history collection for calendar-year analysis.

The relay accepts date-wide ``daily``/``daily_basic`` requests.  ``moneyflow``
is paged at 1,000 rows, then normalized into the project's source-aware flow
tables.  Every date/dataset is committed before the next request starts.
"""

from __future__ import annotations

from datetime import date, datetime
import math
import json
from pathlib import Path
import time
from typing import Any, Iterable

import duckdb

from base import DuckDBStore
from trade_system.schema import init_schema
from trade_system.tushare_store import (collect_tushare_stock_basic, collect_tushare_trade_cal, ts_code_to_stock_code)
from trade_system.flow_contract import ensure_stock_flow_contract, normalize_stock_flow_row
from trade_system.xiaodefa_source import XiaodefaClient, XiaodefaError


CHECKPOINT_DATE = "1900-01-01"
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
DAILY_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv"
ADJ_FACTOR_FIELDS = "ts_code,trade_date,adj_factor"
# Stock order buckets share one request and one atomic snapshot.
MONEYFLOW_MAIN_FIELDS = (
    "ts_code,trade_date,buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount,net_mf_amount"
)
MONEYFLOW_SIZE_FIELDS = (
    "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
    "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount"
)
# DC bucket fields are already net yuan; this endpoint has no sell buckets.
INDUSTRY_FIELDS = (
    "trade_date,content_type,ts_code,name,pct_change,close,net_amount,"
    "net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"
)
STOCK_SNAPSHOT_DATASETS = {"daily", "daily_basic", "adj_factor"}
MIN_STOCK_SNAPSHOT_COVERAGE = 0.99


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


def _pages(client: XiaodefaClient, api: str, params: dict[str, Any], fields: str,
           *, page_size: int, max_pages: int = 10) -> Iterable[tuple[int, list[dict[str, Any]]]]:
    for page_no in range(max_pages):
        query = dict(params)
        query.update(limit=page_size, offset=page_no * page_size)
        rows = client.query_rows(api, query, fields)
        yield page_no, rows
        if len(rows) < page_size:
            break


class TushareHistoryCollector:
    def __init__(self, db_path: str | Path, *, client: XiaodefaClient | None = None,
                 request_timeout: int = 20, retries: int = 3,
                 batch_limit: int = 5000, moneyflow_page_size: int = 1000,
                 budget_seconds: float = 300.0):
        # Validate the only approved client before opening a business database.
        self.client = client if client is not None else XiaodefaClient(
            timeout=request_timeout, max_retries=retries)
        self.db_path = str(db_path)
        self.store = DuckDBStore(self.db_path)
        init_schema(self.store.conn)
        ensure_stock_flow_contract(self.store.conn)
        self._last_source_provider = self._provider_name(self.client)
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

    def _validate_stock_snapshot(self, dataset: str, rows: list[Any], trade_date: str) -> None:
        """Reject an empty/short real close snapshot before publishing it."""
        if not self._is_production_source():
            return
        observed = len(rows)
        if observed <= 0:
            raise XiaodefaError(f"empty {dataset} response for {_iso(trade_date)}")
        expected = self._expected_stock_count()
        # A live listed universe is normally >5,000 rows.  Keep the threshold
        # proportional so a few suspended/unavailable names are acceptable,
        # while a truncated relay page cannot become a successful close.
        if expected >= 1000 and observed < max(1000, math.ceil(expected * MIN_STOCK_SNAPSHOT_COVERAGE)):
            raise XiaodefaError(
                f"incomplete {dataset} response: {observed}/{expected} rows for {_iso(trade_date)}"
            )

    def _expected_stock_count(self) -> int:
        return int(self.store.conn.execute(
            "SELECT count(DISTINCT ts_code) FROM tushare_stock_basic WHERE ts_code IS NOT NULL"
        ).fetchone()[0] or 0)

    def _is_production_source(self) -> bool:
        return isinstance(self.client, XiaodefaClient)

    @staticmethod
    def _provider_name(source: Any) -> str:
        return "xiaodefa" if isinstance(source, XiaodefaClient) else "custom"





    def _is_complete_stock_table(self, table: str, date_column: str, trade_date: str) -> bool:
        expected = self._expected_stock_count()
        if expected < 1000:
            return False
        observed = int(self.store.conn.execute(
            f"SELECT count(DISTINCT ts_code) FROM {table} WHERE {date_column}=? AND ts_code IS NOT NULL",
            [_iso(trade_date)],
        ).fetchone()[0] or 0)
        return observed >= max(1000, math.ceil(expected * MIN_STOCK_SNAPSHOT_COVERAGE))

    def _certify_close_snapshot(self, dataset: str, trade_date: str, *, status: str,
                                error_message: str = "", provider: str = "tushare") -> None:
        table_by_dataset = {
            "daily": ("tushare_daily", "date", "close"),
            "daily_basic": ("tushare_daily_basic", "date", None),
            "adj_factor": ("tushare_adj_factor", "date", "adj_factor"),
        }
        spec = table_by_dataset.get(dataset)
        if not spec:
            return
        table, date_column, value_column = spec
        expected = self._expected_stock_count()
        observed = int(self.store.conn.execute(
            f"SELECT count(*) FROM {table} WHERE {date_column}=?", [_iso(trade_date)]
        ).fetchone()[0] or 0)
        distinct_codes = int(self.store.conn.execute(
            f"SELECT count(DISTINCT ts_code) FROM {table} WHERE {date_column}=? AND ts_code IS NOT NULL",
            [_iso(trade_date)],
        ).fetchone()[0] or 0)
        invalid_rows = 0
        if value_column:
            invalid_rows = int(self.store.conn.execute(
                f"SELECT count(*) FROM {table} WHERE {date_column}=? AND ({value_column} IS NULL OR {value_column} <= 0)",
                [_iso(trade_date)],
            ).fetchone()[0] or 0)
        coverage = round(distinct_codes * 100.0 / expected, 4) if expected else None
        certified = (
            status == "certified"
            and expected >= 1000
            and distinct_codes >= math.ceil(expected * MIN_STOCK_SNAPSHOT_COVERAGE)
            and invalid_rows == 0
        )
        final_status = "certified" if certified else (status if status != "certified" else "incomplete")
        self.store.conn.execute(
            "INSERT INTO close_snapshot_certification "
            "(dataset,trade_date,provider,expected_rows,observed_rows,distinct_codes,invalid_rows,coverage_pct,source_event_date,fetched_at,status,error_message) "
            "VALUES (?,?,?,?,?,?,?,?,CAST(? AS DATE),current_timestamp,?,?) "
            "ON CONFLICT(dataset,trade_date,provider) DO UPDATE SET "
            "expected_rows=excluded.expected_rows,observed_rows=excluded.observed_rows,distinct_codes=excluded.distinct_codes,"
            "invalid_rows=excluded.invalid_rows,coverage_pct=excluded.coverage_pct,source_event_date=excluded.source_event_date,"
            "fetched_at=excluded.fetched_at,status=excluded.status,error_message=excluded.error_message",
            [dataset, _iso(trade_date), provider, expected, observed, distinct_codes, invalid_rows,
             coverage, _iso(trade_date), final_status, error_message[:500]],
        )
        self.store.conn.commit()

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
            # A success checkpoint with zero stored rows means an empty relay
            # response was recorded as success (observed 2026-08-10 on
            # daily/daily_basic).  Treat it as not-done so the next run
            # re-fetches the date instead of permanently skipping it.
            if count == 0:
                return False
            if dataset in STOCK_SNAPSHOT_DATASETS and not self._is_complete_stock_table(table, date_column, trade_date):
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
        """One acquisition; page termination and coverage are independent checks."""
        target = _iso(trade_date)
        params = {"trade_date": _ymd(trade_date)}
        if self._is_production_source():
            rows = self.client.query_all(api, page_size=self.batch_limit,
                                         fields=fields, **params)
        else:
            rows = self.client.query_rows(api, params, fields)
        if any(not r.get("ts_code") or not r.get("trade_date") or
               _iso(r["trade_date"]) != target for r in rows):
            raise XiaodefaError("response contains wrong session or missing identity")
        keys = [str(r["ts_code"]) for r in rows]
        if len(keys) != len(set(keys)):
            raise XiaodefaError("duplicate instrument in snapshot")
        self._validate_stock_snapshot(api, rows, trade_date)
        self._last_source_provider = self._provider_name(self.client)
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
                # Historical relay partitions may reject trade_cal even when
                # the canonical daily table already proves the requested
                # session existed.  Use only those real observed dates as a
                # fail-safe; never synthesize weekdays.
                observed = int(self.store.conn.execute(
                    "SELECT count(DISTINCT date) FROM tushare_daily "
                    "WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)",
                    params,
                ).fetchone()[0] or 0)
                if observed == expected_calendar_days:
                    return [str(row[0])[:10] for row in self.store.conn.execute(
                        "SELECT DISTINCT CAST(date AS VARCHAR) FROM tushare_daily "
                        "WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) ORDER BY date",
                        params,
                    ).fetchall()]
                raise XiaodefaError(
                    f"trade_cal fetch failed for {_iso(start_date)}..{_iso(end_date)}: {exc}"
                ) from exc
            stored_calendar_days = int(self.store.conn.execute(
                "SELECT count(DISTINCT cal_date) FROM tushare_trade_cal "
                "WHERE cal_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)",
                params,
            ).fetchone()[0])
        if stored_calendar_days < expected_calendar_days:
            raise XiaodefaError(
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
                    _num(row.get("vol")), _num(row.get("amount")), _num(row.get("pct_chg")),
                    "hands", "thousand_yuan", "none", self._last_source_provider)
        out = list(deduped.values())
        self._validate_stock_snapshot("daily", out, trade_date)
        # Clear stale/partial rows only after a complete-looking response has
        # arrived, so a failed request never destroys the last usable batch.
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_daily WHERE date=?", [_iso(trade_date)])
            count = self._bulk_replace(
                "tushare_daily", out,
                ["ts_code", "stock_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct", "volume_unit", "amount_unit", "adjustment", "provider"],
                ["ts_code", "date"],
            )
            self.store.conn.execute("COMMIT")
            self._certify_close_snapshot("daily", trade_date, status="certified", provider=self._last_source_provider)
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
        self._validate_stock_snapshot("daily_basic", out, trade_date)
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_daily_basic WHERE date=?", [_iso(trade_date)])
            count = self._bulk_replace(
                "tushare_daily_basic", out,
                ["ts_code", "stock_code", "date", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv"],
                ["ts_code", "date"],
            )
            self.store.conn.execute("COMMIT")
            self._certify_close_snapshot("daily_basic", trade_date, status="certified", provider=self._last_source_provider)
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
        self._validate_stock_snapshot("adj_factor", out, trade_date)
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_adj_factor WHERE date=?", [_iso(trade_date)])
            count = self._bulk_replace(
                "tushare_adj_factor", out,
                ["ts_code", "stock_code", "date", "adj_factor"],
                ["ts_code", "date"],
            )
            self.store.conn.execute("COMMIT")
            self._certify_close_snapshot("adj_factor", trade_date, status="certified", provider=self._last_source_provider)
            return count
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def _collect_moneyflow(self, trade_date: str) -> int:
        # Request the retained provider's full projection once. Missing values stay NULL.
        fields = ",".join(dict.fromkeys((MONEYFLOW_MAIN_FIELDS+","+MONEYFLOW_SIZE_FIELDS).split(",")))
        rows = self._query_date_batch("moneyflow", trade_date, fields)
        out = [(r["ts_code"], ts_code_to_stock_code(r["ts_code"]), _iso(r["trade_date"]),
                *[_num(r.get(k)) for k in ("buy_sm_amount","sell_sm_amount","buy_md_amount",
                  "sell_md_amount","buy_lg_amount","sell_lg_amount","buy_elg_amount",
                  "sell_elg_amount","net_mf_amount")]) for r in rows]
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute("DELETE FROM tushare_moneyflow WHERE date=?", [_iso(trade_date)])
            total = self._bulk_replace("tushare_moneyflow", out,
                ["ts_code","stock_code","date","buy_sm_amount","sell_sm_amount","buy_md_amount",
                 "sell_md_amount","buy_lg_amount","sell_lg_amount","buy_elg_amount","sell_elg_amount","net_mf_amount"],
                ["ts_code","date"])
            self.store.conn.execute("COMMIT")
            return total
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def _collect_industry_flow(self, trade_date: str) -> int:
        api = "moneyflow_ind_dc"
        fields = INDUSTRY_FIELDS
        if self._is_production_source():
            rows = self.client.query_all(api, fields=fields, trade_date=_ymd(trade_date))
        else:
            rows = self.client.query_rows(api, {"trade_date": _ymd(trade_date)}, fields)
        if not rows and self._is_production_source():
            raise XiaodefaError("empty industry flow response")
        if any(_iso(r.get("trade_date")) != _iso(trade_date) for r in rows):
            raise XiaodefaError("industry flow session mismatch")
        rows = [{**r, "source_api":api} for r in rows]
        out = [
            (_iso(row.get("trade_date") or trade_date), row.get("ts_code"), row.get("name"), _num(row.get("pct_change")),
             _num(row.get("close")), _num(row.get("net_amount")), _num(row.get("buy_elg_amount")),
             _num(row.get("sell_elg_amount")), _num(row.get("buy_lg_amount")), _num(row.get("sell_lg_amount")),
             _num(row.get("buy_md_amount")), _num(row.get("sell_md_amount")), _num(row.get("buy_sm_amount")),
             _num(row.get("sell_sm_amount")), _json(row))
            for row in rows if row.get("ts_code")
        ]
        if not out:
            return 0
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
            raw = dict(zip(("buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
                            "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
                            "net_mf_amount"), row[2:]))
            normalized = normalize_stock_flow_row({**raw, "source_api": "moneyflow",
                                                   "amount_unit": "10000_yuan"}, "tushare")
            out.append([_iso(trade_date), row[1], *[normalized[k] for k in (
                        "main_net", "net_total", "super_net", "large_net", "mid_net", "small_net")],
                        "tushare", normalized["amount_unit"], normalized["flow_definition"], "moneyflow",
                        "tushare", normalized["field_mapping_version"], False,
                        _json({**raw, "ts_code": row[0], "source": "tushare_moneyflow", "unit": "10000_yuan"})])
        # An empty source batch is not a valid replacement.  In particular,
        # an upstream timeout can leave the raw table empty while the last
        # verified normalized snapshot is still usable.  Return before the
        # DELETE so the old provider rows survive the transient failure.
        if not out:
            return 0
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
            super_net, large, mid, small = [_num(row[i]) for i in (5, 7, 9, 11)]
            out.append([_iso(trade_date), row[0], row[1], _num(row[4]) if row[4] is not None else None,
                        super_net, large, mid, small, row[2], "tushare_sector_full",
                        "tushare_dc_sector", "yuan", False,
                        _json({"close": row[3], "source": "moneyflow_ind_dc", "unit": "yuan"})])
        if not out:
            return 0
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
            max_days: int | None = None, force: bool = False, gap_only: bool = False,
            retry_passes: int = 0, retry_delay_seconds: float = 0.0) -> dict[str, Any]:
        datasets = list(dict.fromkeys(datasets))
        dates = self.ensure_calendar(start_date, end_date)
        if gap_only:
            dates = [
                trade_date for trade_date in dates
                if any(
                    dataset != "stock_basic" and not self._is_done(dataset, trade_date, force=False)
                    for dataset in datasets
                )
            ]
        if max_days is not None:
            limit = max(0, int(max_days))
            if limit:
                dates = dates[-limit:]
        # Close runs commonly scan a lookback window under a fixed budget.  The
        # newest session is the operational dependency, so process newest first
        # and let the checkpointed history pass repair older gaps afterwards.
        dates = list(reversed(dates))
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
                        if dataset in STOCK_SNAPSHOT_DATASETS:
                            self._certify_close_snapshot(
                                dataset,
                                trade_date,
                                status="error",
                                error_message=str(exc),
                            )
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
