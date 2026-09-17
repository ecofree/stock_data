"""TuShare dataset conversions and storage; transport belongs to XiaodefaClient."""
from __future__ import annotations
from datetime import datetime, timedelta
from base import DuckDBStore
from trade_system.xiaodefa_source import XiaodefaClient
from trade_system.units import _number


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


def collect_tushare_trade_cal(client: XiaodefaClient, store: DuckDBStore, start_date: str, end_date: str) -> int:
    rows = client.query_rows(
        "trade_cal",
        {"exchange": "SSE", "start_date": start_date, "end_date": end_date},
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    out = [
        (
            row.get("exchange") or "SSE",
            _iso_date(row.get("cal_date")),
            (bool(int(row["is_open"])) if str(row.get("is_open")) in {"0", "1"} else None),
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


def collect_tushare_stock_basic(client: XiaodefaClient, store: DuckDBStore) -> int:
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
    client: XiaodefaClient,
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
                    _number(row.get("open")),
                    _number(row.get("high")),
                    _number(row.get("low")),
                    _number(row.get("close")),
                    _number(row.get("vol")),
                    _number(row.get("amount")),
                    _number(row.get("pct_chg")),
                    "hands", "thousand_yuan", "none", "tushare",
                )
                for row in rows
                if _iso_date(row.get("trade_date"))
            ]
            total += store.insert_rows(
                "tushare_daily",
                out,
                ["ts_code", "stock_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct", "volume_unit", "amount_unit", "adjustment", "provider"],
                replace_on=["ts_code", "date"],
            )
    return total


def collect_tushare_daily_basic(
    client: XiaodefaClient,
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
                    _number(row.get("turnover_rate")),
                    _number(row.get("volume_ratio")),
                    _number(row.get("pe")),
                    _number(row.get("pb")),
                    _number(row.get("total_mv")),
                    _number(row.get("circ_mv")),
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
    client: XiaodefaClient,
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
                    _number(row.get("adj_factor")),
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
    client: XiaodefaClient,
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
                    _number(row.get("open")),
                    _number(row.get("high")),
                    _number(row.get("low")),
                    _number(row.get("close")),
                    _number(row.get("vol")),
                    _number(row.get("amount")),
                    _number(row.get("pct_chg")),
                    "hands", "thousand_yuan", "none", "tushare",
                )
                for row in rows
                if _iso_date(row.get("trade_date"))
            ]
            total += store.insert_rows(
                "tushare_index_daily",
                out,
                ["ts_code", "index_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct", "volume_unit", "amount_unit", "adjustment", "provider"],
                replace_on=["ts_code", "date"],
            )
    return total
