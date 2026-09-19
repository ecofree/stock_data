"""Batch, resumable TuShare history collection for calendar-year analysis.

The relay accepts date-wide ``daily``/``daily_basic`` requests.  ``moneyflow``
is paged at 1,000 rows, then normalized into the project's source-aware flow
tables.  Every date/dataset is committed before the next request starts.
"""

from __future__ import annotations

from datetime import date, datetime
import math
import json
import hashlib
from pathlib import Path
import time
from typing import Any, Iterable

import duckdb

from trade_system.data_store import DuckDBStore
from trade_system.schema import init_schema
from trade_system.tushare_store import (
    MARKET_FIELDS, bulk_replace, index_code_to_ts_code, market_batch,
    stock_code_to_ts_code, store_reference, ts_code_to_stock_code,
)
from trade_system.flow_contract import ensure_stock_flow_contract, normalize_stock_flow_row
from trade_system.xiaodefa_source import XiaodefaClient, XiaodefaError


CHECKPOINT_DATE = "1900-01-01"
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


class TushareHistoryCollector:
    def __init__(self, db_path: str | Path, *, client: XiaodefaClient | None = None,
                 request_timeout: int = 20, retries: int = 3,
                 batch_limit: int = 5000, moneyflow_page_size: int = 1000,
                 budget_seconds: float = 300.0, offline: bool = False):
        # Validate the only approved client before opening a business database.
        self.client = client if client is not None else (None if offline else XiaodefaClient(
            timeout=request_timeout, max_retries=retries))
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
        if not rows:
            raise XiaodefaError(f"empty {dataset} response for {_iso(trade_date)}")
        expected = self._expected_stock_codes(trade_date, dataset)
        observed = {r["ts_code"] if isinstance(r, dict) else r[0] for r in rows}
        if dataset == 'moneyflow' and expected - observed:
            self._read_rows('suspend_d', {'trade_date': _ymd(trade_date)},
                            'ts_code,trade_date,suspend_timing,suspend_type')
            expected = self._expected_stock_codes(trade_date, dataset)
        if not expected or expected - observed:
            raise XiaodefaError(f"incomplete {dataset} response: "
                               f"{len(expected - observed)} missing instruments; expected universe={len(expected)}")

    def _expected_stock_codes(self, trade_date, dataset=None):
        expected = {r[0] for r in self.store.conn.execute(
            "SELECT DISTINCT ts_code FROM tushare_stock_basic WHERE ts_code IS NOT NULL "
            "AND (list_date IS NULL OR list_date<=CAST(? AS DATE)) "
            "AND (delist_date IS NULL OR delist_date>CAST(? AS DATE))", [_iso(trade_date)] * 2).fetchall()}
        if dataset == "moneyflow":
            receipts = self.store.conn.execute(
                "SELECT payload_json FROM multi_source_observation WHERE data_type='tushare_suspend_d' "
                "AND provider='xiaodefa' AND json_extract_string(payload_json,'$.params.trade_date')=? "
                "ORDER BY observed_at DESC LIMIT 1", [_ymd(trade_date)]).fetchall()
            for (payload,) in receipts:
                receipt = json.loads(payload)
                if receipt.get('params', {}).get('trade_date') != _ymd(trade_date):
                    continue
                rows = receipt['rows']
                if (len({r.get('ts_code') for r in rows}) != len(rows)
                        or any(r.get('trade_date') != _ymd(trade_date) for r in rows)):
                    break  # Ambiguous suspension evidence cannot shrink the universe.
                suspended = {r['ts_code'] for r in rows if r.get('trade_date') == _ymd(trade_date)
                             and r.get('suspend_type') == 'S' and r.get('suspend_timing') in (None, '')}
                zero = {r[0] for r in self.store.conn.execute(
                    "SELECT ts_code FROM tushare_daily WHERE date=? AND volume=0 AND turnover=0",
                    [_iso(trade_date)]).fetchall()}
                expected -= suspended & zero
                break
        return expected

    def _is_production_source(self) -> bool:
        return isinstance(self.client, XiaodefaClient)

    @staticmethod
    def _provider_name(source: Any) -> str:
        return "xiaodefa" if isinstance(source, XiaodefaClient) else "custom"





    def _is_complete_stock_table(self, table: str, date_column: str, trade_date: str) -> bool:
        expected = self._expected_stock_codes(trade_date, table.removeprefix("tushare_"))
        return bool(expected) and expected <= self._covered_codes(table.removeprefix("tushare_"), trade_date)

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
        expected = len(self._expected_stock_codes(trade_date))
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
            and self._is_complete_stock_table(table, date_column, trade_date)
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
            if dataset in STOCK_SNAPSHOT_DATASETS | {"moneyflow"} and not self._is_complete_stock_table(table, date_column, trade_date):
                return False
        return True

    def _next_attempt(self, dataset: str, trade_date: str) -> int:
        row = self.store.conn.execute(
            "SELECT attempts FROM history_fetch_checkpoint "
            "WHERE dataset=? AND trade_date=? AND page_no=0",
            [dataset, _iso(trade_date)],
        ).fetchone()
        return int((row[0] if row else 0) or 0) + 1

    def _read_rows(self, api, params, fields):
        if self.client is None:
            raise XiaodefaError("offline collector cannot acquire data")
        # Keep the original acquisition time and source identity even when a
        # later page or coverage validation fails. Reuse the existing receipt table.
        def record(offset, rows):
            payload = _json({"source": "tushare", "delivery": self._provider_name(self.client),
                             "api": api, "params": params, "offset": offset, "rows": rows})
            self.store.conn.execute(
                "INSERT INTO multi_source_observation "
                "(data_type,asset_type,asset_code,provider,status,payload_json,payload_hash) "
                "VALUES (?,?,?,?,?,?,?)",
                ["tushare_" + api, "receipt", params.get("ts_code"), self._provider_name(self.client),
                 "received_unverified", payload, hashlib.sha256(payload.encode()).hexdigest()])
        if self._is_production_source():
            return self.client.query_all(api, page_size=self.batch_limit, fields=fields,
                                         on_page=record, **params)
        rows = self.client.query_rows(api, params, fields)
        record(0, rows)
        return rows

    def _collect_reference(self, dataset, start=None, end=None):
        if dataset == "trade_cal":
            params = {"start_date": start, "end_date": end}
            fields = "exchange,cal_date,is_open,pretrade_date"
        else:
            params = {"exchange": "", "list_status": "L"}
            fields = "ts_code,symbol,name,area,industry,market,list_date,list_status,delist_date"
        rows = []
        selectors = ([dict(params, exchange=e) for e in ("SSE", "SZSE")] if dataset == "trade_cal"
                     else [dict(params, list_status=s) for s in ("L", "D")])
        for request in selectors:
            batch = self._read_rows(dataset, request, fields)
            if dataset == "trade_cal":
                days = {_iso(r.get("cal_date")) for r in batch}
                if (len(days) != len(batch) or len(days) != (date.fromisoformat(_iso(end)) - date.fromisoformat(_iso(start))).days + 1
                        or any(r.get("exchange") != request["exchange"] or not _iso(start) <= _iso(r["cal_date"]) <= _iso(end)
                               or str(r.get("is_open")) not in {"0", "1"} for r in batch)):
                    raise XiaodefaError("incomplete or invalid exchange calendar response")
            elif ((request["list_status"] == "L" and not batch) or any(
                    r.get("list_status") != request["list_status"] or
                    (r["list_status"] == "D" and (not r.get("delist_date") or not r.get("list_date")
                     or _iso(r["delist_date"]) <= _iso(r["list_date"]))) for r in batch)):
                raise XiaodefaError("invalid stock lifecycle response")
            rows.extend(batch)
        if not rows:
            raise XiaodefaError(f"empty {dataset} response")
        return store_reference(self.store, dataset, rows)

    def _query_date_batch(self, api: str, trade_date: str, fields: str, *, with_limit: bool = True) -> list[dict[str, Any]]:
        """One acquisition; page termination and coverage are independent checks."""
        target = _iso(trade_date)
        params = {"trade_date": _ymd(trade_date)}
        rows = self._read_rows(api, params, fields)
        if any(not r.get("ts_code") or not r.get("trade_date") or
               _iso(r["trade_date"]) != target for r in rows):
            raise XiaodefaError("response contains wrong session or missing identity")
        keys = [str(r["ts_code"]) for r in rows]
        if len(keys) != len(set(keys)):
            raise XiaodefaError("duplicate instrument in snapshot")
        self._validate_stock_snapshot(api, rows, trade_date)
        self._last_source_provider = self._provider_name(self.client)
        return rows



    def ensure_calendar(self, start_date: str, end_date: str, *, allow_fetch: bool = True) -> list[str]:
        start = date.fromisoformat(_iso(start_date))
        end = date.fromisoformat(_iso(end_date))
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        expected_days = (end - start).days + 1

        def read_calendar():
            result = {}
            for day, opened in self.store.conn.execute(
                "SELECT CAST(cal_date AS VARCHAR),CASE WHEN count(*)=2 AND count(DISTINCT exchange)=2 "
                "AND count(is_open)=2 AND min(is_open)=max(is_open) THEN bool_and(is_open) END "
                "FROM tushare_trade_cal WHERE exchange IN ('SSE','SZSE') "
                "AND cal_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) GROUP BY cal_date",
                [start, end]).fetchall():
                if day in result and result[day] != opened:
                    raise XiaodefaError("conflicting exchange calendar states")
                result[day] = opened
            return result

        calendar = read_calendar()
        if len(calendar) < expected_days or None in calendar.values():
            if not allow_fetch:
                raise XiaodefaError("verified calendar required for offline planning")
            try:
                self._collect_reference("trade_cal", _ymd(start_date), _ymd(end_date))
            except Exception as exc:
                raise XiaodefaError(f"trade_cal fetch failed for {start}..{end}: {exc}") from exc
            calendar = read_calendar()
        if len(calendar) != expected_days or None in calendar.values():
            raise XiaodefaError(f"trade_cal incomplete for {start}..{end}: {len(calendar)}/{expected_days}")
        # Stored prices never substitute for the exchange calendar. Closed and
        # unknown sessions cannot be silently converted into trading days.
        return sorted(day for day, opened in calendar.items() if opened)

    def collect_stock_basic(self, *, force: bool = False) -> int:
        if self._is_done("stock_basic", CHECKPOINT_DATE, force):
            return 0
        self._checkpoint("stock_basic", CHECKPOINT_DATE, "running", attempts=1)
        try:
            rows = self._collect_reference("stock_basic")
            self.store.conn.commit()
            self._checkpoint("stock_basic", CHECKPOINT_DATE, "success", rows=rows, attempts=1)
            return rows
        except Exception as exc:
            self._checkpoint("stock_basic", CHECKPOINT_DATE, "error", attempts=1, error=str(exc))
            raise

    def _applicable_codes(self, codes, trade_date):
        known = {r[0] for r in self.store.conn.execute("SELECT ts_code FROM tushare_stock_basic").fetchall()}
        # Unknown identities remain required; only dated lifecycle facts exclude them.
        return set(codes) - (known - self._expected_stock_codes(trade_date))

    def _covered_codes(self, dataset, trade_date):
        predicate = "TRUE"
        if dataset in {"daily", "index_daily"}:
            predicate = "close>0 AND isfinite(close)"
        elif dataset == "adj_factor":
            predicate = "adj_factor>0 AND isfinite(adj_factor)"
        elif dataset == "moneyflow":
            predicate = "(" + " OR ".join(f"isfinite({f})" for f in
                ("net_mf_amount", "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount")) + ")"
        elif dataset == "daily_basic":
            predicate = "(" + " OR ".join(f"isfinite({f})" for f in
                ("turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv")) + ")"
        return {r[0] for r in self.store.conn.execute(
            f"SELECT ts_code FROM tushare_{dataset} WHERE date=? AND {predicate} GROUP BY ts_code HAVING count(*)=1",
            [_iso(trade_date)]).fetchall()}

    def _collect_market(self, dataset, trade_date, *, codes=None, force=False):
        expected = None if codes is None else (set(codes) if dataset == "index_daily"
                                               else self._applicable_codes(codes, trade_date))
        if expected is None:
            rows = self._query_date_batch(dataset, trade_date, MARKET_FIELDS[dataset])
        else:
            missing = expected if force else expected - self._covered_codes(dataset, trade_date)
            rows = []
            for code in sorted(missing):
                if not self._budget_left():
                    raise XiaodefaError("request budget exhausted; coverage incomplete")
                batch = self._read_rows(dataset, {"ts_code": code, "trade_date": _ymd(trade_date)},
                                        MARKET_FIELDS[dataset])
                if any(r.get("ts_code") != code or _iso(r.get("trade_date")) != _iso(trade_date)
                       for r in batch):
                    raise XiaodefaError("scoped response identity/session mismatch")
                if len(batch) > 1:
                    raise XiaodefaError("duplicate instrument in scoped snapshot")
                rows.extend(batch)
        value = "adj_factor" if dataset == "adj_factor" else "close"
        if dataset != "daily_basic" and any(
            _num(r.get(value)) is None or not math.isfinite(_num(r.get(value))) or _num(r.get(value)) <= 0
            for r in rows
        ):
            raise XiaodefaError("invalid price or adjustment in response")
        if dataset == "daily_basic" and any(not any(
            _num(r.get(f)) is not None and math.isfinite(_num(r.get(f)))
            for f in ("turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv")
        ) for r in rows):
            raise XiaodefaError("daily_basic contains no qualified fields")
        out, columns = market_batch(dataset, rows, self._provider_name(self.client))
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            count = bulk_replace(self.store.conn, "tushare_" + dataset, out, columns, ["ts_code", "date"])
            self.store.conn.execute("COMMIT")
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise
        if expected is not None:
            unresolved = (expected - {r["ts_code"] for r in rows} if force
                          else expected - self._covered_codes(dataset, trade_date))
            if unresolved:
                raise XiaodefaError(f"coverage incomplete: {len(unresolved)} missing instruments; "
                                   "provider absence does not prove suspension")
        else:
            missing = self._expected_stock_codes(trade_date) - self._covered_codes(dataset, trade_date)
            if missing:
                raise XiaodefaError(f"coverage incomplete: {len(missing)} instruments lack qualified fields")
            self._certify_close_snapshot(dataset, trade_date, status="certified",
                                         provider=self._provider_name(self.client))
        return count

    def _collect_daily(self, trade_date):
        return self._collect_market("daily", trade_date)

    def _collect_daily_basic(self, trade_date):
        return self._collect_market("daily_basic", trade_date)

    def _collect_adj_factor(self, trade_date):
        return self._collect_market("adj_factor", trade_date)

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
            total = bulk_replace(self.store.conn, "tushare_moneyflow", out,
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
            count = bulk_replace(self.store.conn,
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
            count = bulk_replace(self.store.conn,
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
            "buy_lg_amount,sell_lg_amount,buy_md_amount,sell_md_amount,buy_sm_amount,sell_sm_amount,raw_json "
            "FROM tushare_moneyflow_industry WHERE trade_date=? AND close IS NOT NULL AND close>0",
            [_iso(trade_date)],
        ).fetchall()
        out = []
        for row in rows:
            raw = json.loads(row[13] or '{}')
            # Unlike stock-level ``moneyflow`` (万元), TuShare's DC industry
            # endpoint documents these fields as yuan.  Do not multiply them
            # again or sector totals become four orders of magnitude too high.
            super_net, large, mid, small = [_num(row[i]) for i in (5, 7, 9, 11)]
            out.append([_iso(trade_date), row[0], row[1], _num(row[4]) if row[4] is not None else None,
                        super_net, large, mid, small, row[2], "tushare_sector_full",
                        {"行业": "em_industry", "概念": "em_concept", "地域": "em_region"}.get(raw.get('content_type'), 'tushare_dc_sector'), "yuan", False,
                        _json({**raw, "close": row[3], "source": "moneyflow_ind_dc", "unit": "yuan"})])
        if not out:
            return 0
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            self.store.conn.execute(
                "DELETE FROM multi_source_sector_flow WHERE source_date=? AND provider IN ('tushare','tushare_sector_full')",
                [_iso(trade_date)],
            )
            count = bulk_replace(self.store.conn,
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
            retry_passes: int = 0, retry_delay_seconds: float = 0.0,
            stock_codes: Iterable[str] | None = None, index_codes: Iterable[str] | None = None,
            plan_only: bool = False) -> dict[str, Any]:
        datasets = list(dict.fromkeys(datasets))
        supported = set(MARKET_FIELDS) | {"stock_basic", "moneyflow", "industry_flow"}
        if not datasets or set(datasets) - supported:
            raise ValueError("unsupported or empty TuShare datasets")
        stocks = None if stock_codes is None else sorted({stock_code_to_ts_code(c) for c in stock_codes})
        indexes = None if index_codes is None else sorted({index_code_to_ts_code(c) for c in index_codes})
        if stocks is not None and (not stocks or set(datasets) - set(MARKET_FIELDS)):
            raise ValueError("stock scope requires nonempty codes and only daily/basic/adjustment/index datasets")
        if "index_daily" in datasets and not indexes:
            raise ValueError("index_daily requires explicit index_codes")
        scopes = {d: indexes if d == "index_daily" else stocks for d in datasets if d in MARKET_FIELDS}

        def checkpoint_key(dataset):
            codes = scopes.get(dataset)
            if codes is None:
                return dataset
            identity = hashlib.sha256(_json(codes).encode()).hexdigest()
            return f"{dataset}:scope:{identity}"

        def is_done(dataset, trade_date):
            codes = scopes.get(dataset)
            if codes is None:
                return self._is_done(dataset, trade_date, force)
            expected = set(codes) if dataset == "index_daily" else self._applicable_codes(codes, trade_date)
            return not force and expected <= self._covered_codes(dataset, trade_date)

        dates = self.ensure_calendar(start_date, end_date, allow_fetch=not plan_only)
        if gap_only:
            dates = [
                trade_date for trade_date in dates
                if any(
                    dataset != "stock_basic" and not is_done(dataset, trade_date)
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
        if "stock_basic" in datasets:
            reference = {"dataset": "stock_basic", "trade_date": CHECKPOINT_DATE}
            try:
                if plan_only:
                    reference["status"] = "planned"
                elif not self._budget_left():
                    reference["status"] = "budget_exhausted"
                else:
                    reference["status"] = "success" if self.collect_stock_basic(force=force) else "skipped"
            except Exception as exc:
                reference.update(status="error", error=str(exc)[:240])
            results_by_key[("stock_basic", CHECKPOINT_DATE)] = reference
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
                    if dataset == "stock_basic" or dataset not in (set(handlers) | set(MARKET_FIELDS)):
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
                    if plan_only:
                        codes = scopes.get(dataset)
                        expected = None if codes is None else (set(codes) if dataset == "index_daily"
                            else self._applicable_codes(codes, trade_date))
                        results_by_key[key] = {"dataset": dataset, "trade_date": trade_date,
                            "status": "covered" if is_done(dataset, trade_date) else "planned",
                            "missing_codes": sorted(expected - self._covered_codes(dataset, trade_date))
                                             if expected is not None else None,
                            "not_listed_codes": sorted(set(codes) - expected) if expected is not None else []}
                        continue
                    if is_done(dataset, trade_date):
                        results_by_key.setdefault(key, {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "skipped",
                        })
                        continue
                    checkpoint = checkpoint_key(dataset)
                    attempt = self._next_attempt(checkpoint, trade_date)
                    self._checkpoint(checkpoint, trade_date, "running", attempts=attempt)
                    try:
                        rows = (self._collect_market(dataset, trade_date, codes=scopes.get(dataset), force=force)
                                if dataset in MARKET_FIELDS else handlers[dataset](trade_date))
                        if dataset == "moneyflow":
                            if self._expected_stock_codes(trade_date, dataset) - self._covered_codes(dataset, trade_date):
                                raise XiaodefaError("moneyflow coverage incomplete; missing values are not qualified facts")
                            rows = self.sync_stock_flow(trade_date)
                        elif dataset == "industry_flow":
                            rows = self.sync_sector_flow(trade_date)
                        if dataset in {"moneyflow", "industry_flow"} and rows <= 0:
                            raise XiaodefaError("no qualified flow rows; request is not complete")
                        self._checkpoint(checkpoint, trade_date, "success", rows=rows, attempts=attempt)
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
                        self._checkpoint(checkpoint, trade_date, "error", attempts=attempt, error=str(exc))
                        if dataset in STOCK_SNAPSHOT_DATASETS and scopes.get(dataset) is None:
                            self._certify_close_snapshot(
                                dataset,
                                trade_date,
                                status="error",
                                error_message=str(exc),
                                provider=self._provider_name(self.client),
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
            "tushare_index_daily": "max(date)",
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
