"""Index data collectors for real index list, intraday, full-info, and K-line feeds."""

from __future__ import annotations

import json

from base import DuckDBStore, KPLClient


def _items(data, *keys: str) -> list:
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys + ("data", "items", "list", "indexes", "indices", "klines"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if data:
            return [data]
    return []


def _float(value, default=0.0) -> float:
    try:
        text = str(value).strip().replace(",", "").replace("%", "")
        return float(text if text else default)
    except (TypeError, ValueError):
        return default


def _int(value, default=0) -> int:
    try:
        return int(float(value if value is not None else default))
    except (TypeError, ValueError):
        return default


def _date(value, fallback: str) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text or fallback


def _index_code(item: dict, fallback: str = "") -> str:
    return str(
        item.get("index_code") or item.get("stock_id") or item.get("code")
        or item.get("指数代码") or item.get("指数代码") or fallback or ""
    )


def collect_index_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/index/list", {"date": date})
    rows = []
    for item in _items(data, "indexes", "indices"):
        if not isinstance(item, dict):
            continue
        code = _index_code(item)
        if not code:
            continue
        rows.append(
            (
                _date(item.get("date") or item.get("日期"), date),
                code,
                item.get("index_name") or item.get("name") or item.get("指数名称") or "",
                _float(item.get("price", item.get("value", item.get("最新价")))),
                _float(item.get("change_pct", item.get("涨跌幅"))),
                _float(item.get("change_amt", item.get("涨跌额"))),
                _int(item.get("turnover", item.get("amount", item.get("成交额(元)")))),
                _int(item.get("volume", item.get("成交量"))),
            )
        )
    if rows:
        n = store.insert_rows(
            "index_list",
            rows,
            ["date", "index_code", "index_name", "price", "change_pct", "change_amt", "turnover", "volume"],
            replace_on=["date", "index_code"],
        )
        store.log_collect("index_list", "/index/list", n, "ok")
        return n
    if data:
        store.insert_raw("/index/list", data)
    return 0


def collect_index_intraday(client: KPLClient, store: DuckDBStore, date: str, index_codes: list[str]) -> int:
    total = 0
    for code in index_codes:
        data = client.get("/index/intraday", {"code": code, "date": date})
        rows = []
        for item in _items(data, "intraday", "points"):
            if not isinstance(item, dict):
                continue
            rows.append(
                (
                    date,
                    code,
                    str(item.get("time") or item.get("t") or ""),
                    _float(item.get("price", item.get("p"))),
                    _float(item.get("avg_price", item.get("avg"))),
                    _int(item.get("volume", item.get("v"))),
                    _int(item.get("turnover", item.get("amount"))),
                )
            )
        if rows:
            total += store.insert_rows(
                "index_intraday",
                rows,
                ["date", "index_code", "time", "price", "avg_price", "volume", "turnover"],
                replace_on=["date", "index_code", "time"],
            )
    if total:
        store.log_collect("index_intraday", "/index/intraday", total, "ok")
    return total


def collect_index_full_info(client: KPLClient, store: DuckDBStore, date: str, index_codes: list[str]) -> int:
    rows = []
    for code in index_codes:
        data = client.get("/index/full-info", {"code": code, "date": date})
        if not data:
            continue
        rows.append((date, code, json.dumps(data, ensure_ascii=False)))
    if rows:
        n = store.insert_rows(
            "index_full_info",
            rows,
            ["date", "section", "raw_json"],
            replace_on=["date", "section"],
        )
        store.log_collect("index_full_info", "/index/full-info", n, "ok")
        return n
    return 0


def collect_index_kline(client: KPLClient, store: DuckDBStore, date: str, index_codes: list[str]) -> int:
    total = 0
    for code in index_codes:
        data = client.get("/index/zhishu-kline", {"code": code})
        rows = []
        for item in _items(data, "klines"):
            if not isinstance(item, dict):
                continue
            trade_date = _date(item.get("date"), date)
            rows.append(
                (
                    trade_date,
                    code,
                    _float(item.get("open", item.get("o"))),
                    _float(item.get("high", item.get("h"))),
                    _float(item.get("low", item.get("l"))),
                    _float(item.get("close", item.get("c"))),
                    _int(item.get("volume", item.get("v"))),
                    _int(item.get("turnover", item.get("amount"))),
                    _float(item.get("change_pct", item.get("pct"))),
                    "D",
                )
            )
        if rows:
            total += store.insert_rows(
                "index_kline",
                rows,
                ["date", "index_code", "open", "high", "low", "close", "volume", "turnover", "change_pct", "ktype"],
                replace_on=["date", "index_code", "ktype"],
            )
    if total:
        store.log_collect("index_kline", "/index/zhishu-kline", total, "ok")
    return total


def collect_all_index(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    results = {"index_list": collect_index_list(client, store, date)}
    try:
        index_codes = [row[0] for row in store.fetchall("SELECT DISTINCT index_code FROM index_list WHERE date = ? ORDER BY index_code", [date])]
    except Exception:
        index_codes = []
    if not index_codes:
        index_codes = ["SH000001", "SZ399001", "SZ399006", "SH000688"]
    index_codes = index_codes[:6]
    results["index_intraday"] = collect_index_intraday(client, store, date, index_codes)
    results["index_full_info"] = collect_index_full_info(client, store, date, index_codes)
    results["index_kline"] = collect_index_kline(client, store, date, index_codes)
    return results
