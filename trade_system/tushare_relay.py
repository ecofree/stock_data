"""TuShare relay adapter for basic historical data.

The relay is used as a bounded base-data supplement. It must not be used as an
automatic trading trigger; collectors write staging tables first, then an
explicit sync can copy OHLC rows into the existing core K-line tables.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable
from trade_system.host_limiter import shared_host_limiter

from base import DuckDBStore
from config import SETTINGS
from trade_system.backfill import TABLE_SPECS


# The historical collector must use the same migrated relay as the live
# multi-source entry points.  The previous default was the retired
# Cloudflare-fronted endpoint and made every YTD backfill look like a data
# outage even though the replacement relay is available.
DEFAULT_RELAY_URL = "https://fastapic.stockai888.top"
DEFAULT_RESOLVE = ""
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class TushareRelayError(RuntimeError):
    """Raised when the relay cannot return structured data."""


Runner = Callable[[dict[str, Any], int], dict[str, Any]]


def _settings_value(name: str, default: str = "") -> str:
    return os.environ.get(name) or SETTINGS.get(name, default)


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    return int(_to_float(value, default))


def _iso_date(value) -> str | None:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _compact_date(value: str) -> str:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 8:
        return raw[:8]
    raise ValueError(f"invalid date: {value}")


def _date_windows(start_date: str, end_date: str, max_days: int = 365) -> list[tuple[str, str]]:
    start = datetime.strptime(_compact_date(start_date), "%Y%m%d").date()
    end = datetime.strptime(_compact_date(end_date), "%Y%m%d").date()
    if start > end:
        return []
    windows: list[tuple[str, str]] = []
    current = start
    step = max(1, int(max_days))
    while current <= end:
        window_end = min(current + timedelta(days=step - 1), end)
        windows.append((current.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")))
        current = window_end + timedelta(days=1)
    return windows


def stock_code_to_ts_code(code: str) -> str:
    value = str(code or "").strip().upper()
    if "." in value:
        digits, suffix = value.split(".", 1)
        digits = "".join(ch for ch in digits if ch.isdigit()).zfill(6)[-6:]
        return f"{digits}.{suffix}"
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits and len(digits) <= 6:
        digits = digits.zfill(6)
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def ts_code_to_stock_code(ts_code: str) -> str:
    return str(ts_code or "").split(".", 1)[0]


def index_code_to_ts_code(code: str) -> str:
    value = str(code or "").strip().upper()
    if "." in value:
        return value
    if value.startswith(("SH", "SZ", "BJ")) and len(value) > 2:
        return f"{value[2:]}.{value[:2]}"
    return stock_code_to_ts_code(value)


def ts_code_to_index_code(ts_code: str) -> str:
    value = str(ts_code or "").strip().upper()
    if "." not in value:
        return value
    digits, suffix = value.split(".", 1)
    return f"{suffix}{digits}"


def _stock_code_filter_values(codes: list[str] | None) -> list[str]:
    return [ts_code_to_stock_code(stock_code_to_ts_code(code)) for code in (codes or []) if str(code or "").strip()]


def _index_code_filter_values(codes: list[str] | None) -> list[str]:
    return [ts_code_to_index_code(index_code_to_ts_code(code)) for code in (codes or []) if str(code or "").strip()]


def _append_scope_filters(
    filters: list[str],
    params: list[Any],
    *,
    code_col: str,
    codes: list[str],
    start_date: str | None,
    end_date: str | None,
) -> None:
    if codes:
        placeholders = ", ".join(["?"] * len(codes))
        filters.append(f"{code_col} IN ({placeholders})")
        params.extend(codes)
    if start_date:
        filters.append("date >= CAST(? AS DATE)")
        params.append(_iso_date(start_date))
    if end_date:
        filters.append("date <= CAST(? AS DATE)")
        params.append(_iso_date(end_date))


class TushareRelayClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        url: str | None = None,
        resolve: str | None = None,
        timeout: int = 40,
        retries: int = 3,
        runner: Runner | None = None,
        min_interval_seconds: float | None = None,
    ) -> None:
        configured_token = token if token is not None else (
            _settings_value("TUSHARE_FAST_RELAY_TOKEN")
            or _settings_value("TUSHARE_RELAY_TOKEN")
            or _settings_value("TUSHARE_TOKEN")
        )
        self.token = str(configured_token or "").strip()
        configured_url = url or (
            _settings_value("TUSHARE_FAST_RELAY_URL")
            or _settings_value("TUSHARE_RELAY_URL", DEFAULT_RELAY_URL)
        )
        self.url = str(configured_url or DEFAULT_RELAY_URL).strip()
        configured_resolve = resolve if resolve is not None else _settings_value("TUSHARE_RELAY_RESOLVE", DEFAULT_RESOLVE)
        self.resolve = str(configured_resolve or "").strip()
        self.timeout = int(timeout)
        self.retries = int(retries)
        self._shared_limit_enabled = runner is None
        self.runner = runner or self._run_curl
        if min_interval_seconds is None:
            # The relay allows 100 requests/minute.  Keep a 0.65s
            # start-to-start interval in production to leave transport jitter
            # below the advertised ceiling,
            # while test runners remain fast unless they opt into the limiter.
            min_interval_seconds = (
                "0"
                if runner is not None
                else _settings_value("TUSHARE_RELAY_MIN_INTERVAL_SECONDS", "0.65")
            )
        try:
            self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        except (TypeError, ValueError):
            self.min_interval_seconds = 0.65 if runner is None else 0.0
        self._rate_lock = threading.Lock()
        self._last_request_at: float | None = None

    def _wait_for_rate_limit(self) -> None:
        """Throttle request starts to stay below the relay's 100/minute limit."""
        if self.min_interval_seconds <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                remaining = self.min_interval_seconds - (now - self._last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request_at = time.monotonic()
        # The relay limit is account-wide, not process-wide.  Keep the
        # advertised 100/minute ceiling across all collectors on this machine.
        if self._shared_limit_enabled and self.min_interval_seconds > 0:
            shared_host_limiter.acquire("tushare_relay", self.min_interval_seconds)

    def query(self, api_name: str, params: dict[str, Any] | None = None, fields: str = "") -> tuple[list[str], list[list[Any]]]:
        if not self.token:
            raise TushareRelayError("missing TUSHARE_FAST_RELAY_TOKEN/TUSHARE_RELAY_TOKEN")
        body = {
            "api_name": api_name,
            "token": self.token,
            "params": params or {},
            "fields": fields or "",
        }
        last_error = "unknown"
        for attempt in range(1, self.retries + 1):
            try:
                self._wait_for_rate_limit()
                payload = self.runner(body, self.timeout)
                if payload.get("code") != 0:
                    raise TushareRelayError(f"relay api error: code={payload.get('code')} msg={payload.get('msg', '')}")
                data = payload.get("data") or {}
                return list(data.get("fields") or []), list(data.get("items") or [])
            except Exception as exc:
                last_error = type(exc).__name__
                if attempt >= self.retries:
                    break
                # Rate-limit responses (relay app code=-1 "次数超限" or HTTP
                # 429) only clear once the one-minute window rolls over, so a
                # one-second retry just burns another attempt.  Scale the
                # backoff with the attempt count, capped near the window size.
                text = str(exc)
                if "超限" in text or "429" in text:
                    time.sleep(min(65.0, 15.0 * attempt))
                else:
                    time.sleep(1)
        raise TushareRelayError(f"relay request failed after {self.retries} attempts: {last_error}")

    def query_rows(self, api_name: str, params: dict[str, Any] | None = None, fields: str = "") -> list[dict[str, Any]]:
        field_names, items = self.query(api_name, params, fields)
        rows = []
        for item in items:
            values = list(item)
            if len(values) < len(field_names):
                values.extend([None] * (len(field_names) - len(values)))
            rows.append(dict(zip(field_names, values)))
        return rows

    def _run_curl(self, body: dict[str, Any], timeout: int) -> dict[str, Any]:
        curl = "curl.exe" if os.name == "nt" else "curl"
        cmd = [
            curl,
            "-s",
            "-m",
            str(max(timeout, 5)),
            "-X",
            "POST",
            self.url,
            "-H",
            "Content-Type: application/json",
            "-H",
            f"User-Agent: {DEFAULT_UA}",
            "-H",
            "Origin: https://fastapic.stockai888.top",
            "-H",
            "Referer: https://fastapic.stockai888.top/",
            # Read the JSON body from stdin so the relay token never appears
            # in the Windows process command line.
            "-d",
            "@-",
        ]
        if self.resolve:
            cmd[7:7] = ["--resolve", self.resolve]
        proc = subprocess.run(
            cmd,
            input=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout + 5,
        )
        if proc.returncode != 0:
            raise TushareRelayError(f"curl failed: rc={proc.returncode}")
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise TushareRelayError("relay returned non-json response") from exc


def _row_value(row: dict[str, Any], name: str, default=None):
    value = row.get(name)
    return default if value is None else value


def collect_tushare_trade_cal(client: TushareRelayClient, store: DuckDBStore, start_date: str, end_date: str) -> int:
    rows = client.query_rows(
        "trade_cal",
        {"exchange": "SSE", "start_date": start_date, "end_date": end_date},
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    out = [
        (
            row.get("exchange") or "SSE",
            _iso_date(row.get("cal_date")),
            bool(_to_int(row.get("is_open"))),
            _iso_date(row.get("pretrade_date")),
        )
        for row in rows
        if _iso_date(row.get("cal_date"))
    ]
    return store.insert_rows(
        "tushare_trade_cal",
        out,
        ["exchange", "cal_date", "is_open", "pretrade_date"],
        replace_on=["exchange", "cal_date"],
    )


def collect_tushare_stock_basic(client: TushareRelayClient, store: DuckDBStore) -> int:
    rows = client.query_rows(
        "stock_basic",
        {"exchange": "", "list_status": "L"},
        fields="ts_code,symbol,name,area,industry,market,list_date",
    )
    out = [
        (
            row.get("ts_code"),
            row.get("symbol") or ts_code_to_stock_code(row.get("ts_code")),
            row.get("name") or "",
            row.get("area") or "",
            row.get("industry") or "",
            row.get("market") or "",
            _iso_date(row.get("list_date")),
        )
        for row in rows
        if row.get("ts_code")
    ]
    return store.insert_rows(
        "tushare_stock_basic",
        out,
        ["ts_code", "stock_code", "stock_name", "area", "industry", "market", "list_date"],
        replace_on=["ts_code"],
    )


def collect_tushare_daily(
    client: TushareRelayClient,
    store: DuckDBStore,
    stock_codes: list[str],
    start_date: str,
    end_date: str,
) -> int:
    total = 0
    fields = "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
    for code in stock_codes:
        ts_code = stock_code_to_ts_code(code)
        for window_start, window_end in _date_windows(start_date, end_date):
            rows = client.query_rows(
                "daily",
                {"ts_code": ts_code, "start_date": window_start, "end_date": window_end},
                fields=fields,
            )
            out = [
                (
                    row.get("ts_code") or ts_code,
                    ts_code_to_stock_code(row.get("ts_code") or ts_code),
                    _iso_date(row.get("trade_date")),
                    _to_float(row.get("open")),
                    _to_float(row.get("high")),
                    _to_float(row.get("low")),
                    _to_float(row.get("close")),
                    _to_float(row.get("vol")),
                    _to_float(row.get("amount")),
                    _to_float(row.get("pct_chg")),
                )
                for row in rows
                if _iso_date(row.get("trade_date"))
            ]
            total += store.insert_rows(
                "tushare_daily",
                out,
                ["ts_code", "stock_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
                replace_on=["ts_code", "date"],
            )
    return total


def collect_tushare_daily_basic(
    client: TushareRelayClient,
    store: DuckDBStore,
    stock_codes: list[str],
    start_date: str,
    end_date: str,
) -> int:
    total = 0
    fields = "ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv"
    for code in stock_codes:
        ts_code = stock_code_to_ts_code(code)
        for window_start, window_end in _date_windows(start_date, end_date):
            rows = client.query_rows(
                "daily_basic",
                {"ts_code": ts_code, "start_date": window_start, "end_date": window_end},
                fields=fields,
            )
            out = [
                (
                    row.get("ts_code") or ts_code,
                    ts_code_to_stock_code(row.get("ts_code") or ts_code),
                    _iso_date(row.get("trade_date")),
                    _to_float(row.get("turnover_rate")),
                    _to_float(row.get("volume_ratio")),
                    _to_float(row.get("pe")),
                    _to_float(row.get("pb")),
                    _to_float(row.get("total_mv")),
                    _to_float(row.get("circ_mv")),
                )
                for row in rows
                if _iso_date(row.get("trade_date"))
            ]
            total += store.insert_rows(
                "tushare_daily_basic",
                out,
                ["ts_code", "stock_code", "date", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv"],
                replace_on=["ts_code", "date"],
            )
    return total


def collect_tushare_adj_factor(
    client: TushareRelayClient,
    store: DuckDBStore,
    stock_codes: list[str],
    start_date: str,
    end_date: str,
) -> int:
    total = 0
    fields = "ts_code,trade_date,adj_factor"
    for code in stock_codes:
        ts_code = stock_code_to_ts_code(code)
        for window_start, window_end in _date_windows(start_date, end_date):
            rows = client.query_rows(
                "adj_factor",
                {"ts_code": ts_code, "start_date": window_start, "end_date": window_end},
                fields=fields,
            )
            out = [
                (
                    row.get("ts_code") or ts_code,
                    ts_code_to_stock_code(row.get("ts_code") or ts_code),
                    _iso_date(row.get("trade_date")),
                    _to_float(row.get("adj_factor")),
                )
                for row in rows
                if _iso_date(row.get("trade_date"))
            ]
            total += store.insert_rows(
                "tushare_adj_factor",
                out,
                ["ts_code", "stock_code", "date", "adj_factor"],
                replace_on=["ts_code", "date"],
            )
    return total


def collect_tushare_index_daily(
    client: TushareRelayClient,
    store: DuckDBStore,
    index_codes: list[str],
    start_date: str,
    end_date: str,
) -> int:
    total = 0
    fields = "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
    for code in index_codes:
        ts_code = index_code_to_ts_code(code)
        for window_start, window_end in _date_windows(start_date, end_date):
            rows = client.query_rows(
                "index_daily",
                {"ts_code": ts_code, "start_date": window_start, "end_date": window_end},
                fields=fields,
            )
            out = [
                (
                    row.get("ts_code") or ts_code,
                    ts_code_to_index_code(row.get("ts_code") or ts_code),
                    _iso_date(row.get("trade_date")),
                    _to_float(row.get("open")),
                    _to_float(row.get("high")),
                    _to_float(row.get("low")),
                    _to_float(row.get("close")),
                    _to_float(row.get("vol")),
                    _to_float(row.get("amount")),
                    _to_float(row.get("pct_chg")),
                )
                for row in rows
                if _iso_date(row.get("trade_date"))
            ]
            total += store.insert_rows(
                "tushare_index_daily",
                out,
                ["ts_code", "index_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
                replace_on=["ts_code", "date"],
            )
    return total


def sync_tushare_ohlc_to_core_tables(
    db_path: str | Path,
    *,
    stock_codes: list[str] | None = None,
    index_codes: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, int]:
    store = DuckDBStore(str(db_path))
    try:
        for spec_name in ("kline", "index_kline"):
            store.conn.execute(TABLE_SPECS[spec_name]["ddl"])
        stock_codes_normalized = _stock_code_filter_values(stock_codes)
        index_codes_normalized = _index_code_filter_values(index_codes)

        def _scope(column: str, codes: list[str]) -> tuple[str, list[Any]]:
            filters = [f"{column} IS NOT NULL", f"{column} != ''"]
            params: list[Any] = []
            _append_scope_filters(
                filters,
                params,
                code_col=column,
                codes=codes,
                start_date=start_date,
                end_date=end_date,
            )
            return " AND ".join(filters), params

        stock_where, stock_params = _scope("stock_code", stock_codes_normalized)
        index_where, index_params = _scope("index_code", index_codes_normalized)
        stock_count = store.conn.execute(
            f"SELECT count(*) FROM tushare_daily WHERE {stock_where}", stock_params
        ).fetchone()[0]
        index_count = store.conn.execute(
            f"SELECT count(*) FROM tushare_index_daily WHERE {index_where}", index_params
        ).fetchone()[0]
        # Publish only keys present in the verified source batch.  Empty or
        # partial relay responses must never erase an existing core row.
        if stock_count or index_count:
            store.conn.execute("BEGIN TRANSACTION")
            try:
                if stock_count:
                    store.conn.execute(
                        f"""
                        MERGE INTO kline AS target
                        USING (
                            SELECT *, row_number() OVER (
                                PARTITION BY date, stock_code ORDER BY fetched_at DESC NULLS LAST
                            ) AS _rn
                            FROM tushare_daily
                            WHERE {stock_where}
                        ) AS source
                        ON target.date=source.date
                           AND target.stock_code=source.stock_code
                           AND target.ktype='D'
                        WHEN MATCHED AND source._rn=1 THEN UPDATE SET
                            open=source.open,
                            high=source.high,
                            low=source.low,
                            close=source.close,
                            volume=CAST(source.volume AS BIGINT),
                            turnover=CAST(source.turnover AS BIGINT),
                            change_pct=source.change_pct
                        WHEN NOT MATCHED AND source._rn=1 THEN INSERT
                            (date,stock_code,open,high,low,close,volume,turnover,change_pct,ktype)
                        VALUES
                            (source.date,source.stock_code,source.open,source.high,source.low,source.close,
                             CAST(source.volume AS BIGINT),CAST(source.turnover AS BIGINT),source.change_pct,'D')
                        """,
                        stock_params,
                    )
                if index_count:
                    store.conn.execute(
                        f"""
                        MERGE INTO index_kline AS target
                        USING (
                            SELECT *, row_number() OVER (
                                PARTITION BY date, index_code ORDER BY fetched_at DESC NULLS LAST
                            ) AS _rn
                            FROM tushare_index_daily
                            WHERE {index_where}
                        ) AS source
                        ON CAST(target.date AS VARCHAR)=CAST(source.date AS VARCHAR)
                           AND target.index_code=source.index_code
                           AND target.ktype='D'
                        WHEN MATCHED AND source._rn=1 THEN UPDATE SET
                            open=source.open,
                            high=source.high,
                            low=source.low,
                            close=source.close,
                            volume=CAST(source.volume AS BIGINT),
                            turnover=CAST(source.turnover AS BIGINT),
                            change_pct=source.change_pct
                        WHEN NOT MATCHED AND source._rn=1 THEN INSERT
                            (date,index_code,open,high,low,close,volume,turnover,change_pct,ktype)
                        VALUES
                            (CAST(source.date AS VARCHAR),source.index_code,source.open,source.high,source.low,
                             source.close,CAST(source.volume AS BIGINT),CAST(source.turnover AS BIGINT),
                             source.change_pct,'D')
                        """,
                        index_params,
                    )
                # Keep the source-aware K-line layer aligned with the
                # verified TuShare staging tables.  The production views use
                # ``tushare_daily`` directly, but stale rows in
                # ``multi_source_kline`` made the multi-source audit report a
                # false K-line outage and left the fallback graph split across
                # two authorities.  This is a local MERGE only; it does not
                # trigger another network request.
                source_tables = {
                    row[0] for row in store.conn.execute("SHOW TABLES").fetchall()
                }
                if "multi_source_kline" in source_tables:
                    if stock_count:
                        store.conn.execute(
                            f"""
                            MERGE INTO multi_source_kline AS target
                            USING (
                                SELECT source_date, asset_type, asset_code, open, high, low,
                                       close, volume, amount, change_pct, provider, fetched_at,
                                       is_stale, raw_json
                                FROM (
                                    SELECT date AS source_date, 'stock' AS asset_type,
                                           stock_code AS asset_code, open, high, low, close,
                                           volume, turnover AS amount, change_pct,
                                           'tushare' AS provider, fetched_at,
                                           FALSE AS is_stale, NULL::VARCHAR AS raw_json,
                                           row_number() OVER (
                                               PARTITION BY date, stock_code
                                               ORDER BY fetched_at DESC NULLS LAST
                                           ) AS _rn
                                    FROM tushare_daily
                                    WHERE {stock_where}
                                ) ranked
                                WHERE _rn=1
                            ) AS source
                            ON target.source_date=source.source_date
                               AND target.asset_type=source.asset_type
                               AND target.asset_code=source.asset_code
                               AND target.provider=source.provider
                            WHEN MATCHED THEN UPDATE SET
                                open=source.open, high=source.high, low=source.low,
                                close=source.close, volume=source.volume, amount=source.amount,
                                change_pct=source.change_pct, fetched_at=source.fetched_at,
                                is_stale=FALSE, raw_json=source.raw_json
                            WHEN NOT MATCHED THEN INSERT (
                                source_date, asset_type, asset_code, open, high, low, close,
                                volume, amount, change_pct, provider, fetched_at, is_stale, raw_json
                            ) VALUES (
                                source.source_date, source.asset_type, source.asset_code,
                                source.open, source.high, source.low, source.close,
                                source.volume, source.amount, source.change_pct, source.provider,
                                source.fetched_at, source.is_stale, source.raw_json
                            )
                            """,
                            stock_params,
                        )
                    if index_count:
                        store.conn.execute(
                            f"""
                            MERGE INTO multi_source_kline AS target
                            USING (
                                SELECT source_date, asset_type, asset_code, open, high, low,
                                       close, volume, amount, change_pct, provider, fetched_at,
                                       is_stale, raw_json
                                FROM (
                                    SELECT date AS source_date, 'index' AS asset_type,
                                           index_code AS asset_code, open, high, low, close,
                                           volume, turnover AS amount, change_pct,
                                           'tushare' AS provider, fetched_at,
                                           FALSE AS is_stale, NULL::VARCHAR AS raw_json,
                                           row_number() OVER (
                                               PARTITION BY date, index_code
                                               ORDER BY fetched_at DESC NULLS LAST
                                           ) AS _rn
                                    FROM tushare_index_daily
                                    WHERE {index_where}
                                ) ranked
                                WHERE _rn=1
                            ) AS source
                            ON target.source_date=source.source_date
                               AND target.asset_type=source.asset_type
                               AND target.asset_code=source.asset_code
                               AND target.provider=source.provider
                            WHEN MATCHED THEN UPDATE SET
                                open=source.open, high=source.high, low=source.low,
                                close=source.close, volume=source.volume, amount=source.amount,
                                change_pct=source.change_pct, fetched_at=source.fetched_at,
                                is_stale=FALSE, raw_json=source.raw_json
                            WHEN NOT MATCHED THEN INSERT (
                                source_date, asset_type, asset_code, open, high, low, close,
                                volume, amount, change_pct, provider, fetched_at, is_stale, raw_json
                            ) VALUES (
                                source.source_date, source.asset_type, source.asset_code,
                                source.open, source.high, source.low, source.close,
                                source.volume, source.amount, source.change_pct, source.provider,
                                source.fetched_at, source.is_stale, source.raw_json
                            )
                            """,
                            index_params,
                        )
                store.conn.execute("COMMIT")
            except Exception:
                store.conn.execute("ROLLBACK")
                raise
        return {"kline": int(stock_count or 0), "index_kline": int(index_count or 0)}
    finally:
        store.close()
