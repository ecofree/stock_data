"""Advanced stock-level data collectors."""

import json

from base import KPLClient, DuckDBStore, logger


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    return int(_to_float(value, default))


def _items(data, *keys: str) -> list:
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys + ("data", "items", "list", "klines", "points", "monitors"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if any(key in data for key in ("time", "date", "big_net_amount", "main_activity_score")):
            return [data]
    return []


def _field(item: dict, *names, default=None):
    for name in names:
        if item.get(name) is not None:
            return item.get(name)
    return default


def _compact_date(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _matches_trade_date(item: dict, date: str) -> bool:
    item_date = _field(item, "date", "trade_date", "day", "dt", "datetime", "time", default=None)
    if item_date is None:
        return True
    compact = _compact_date(item_date)
    return len(compact) < 8 or compact == _compact_date(date)


def collect_advanced_pankou(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """Collect best five bid/ask ladder evidence."""
    total = 0
    for code in stock_codes:
        data = client.get("/advanced/pankou", {"code": code})
        if not data:
            continue
        weituo = data.get("weituo", {}) if isinstance(data, dict) else {}
        if not weituo:
            row = (date, code, json.dumps(data, ensure_ascii=False))
            total += store.insert_rows(
                "advanced_pankou",
                [row],
                ["date", "stock_code", "raw_json"],
                replace_on=["date", "stock_code"],
            )
            continue

        def wget(key, idx):
            entry = weituo.get(key, [0, 0])
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                return entry[0] if idx == 0 else entry[1]
            return 0

        row = (
            date,
            code,
            wget("b1", 0),
            wget("b1", 1),
            wget("b2", 0),
            wget("b2", 1),
            wget("b3", 0),
            wget("b3", 1),
            wget("b4", 0),
            wget("b4", 1),
            wget("b5", 0),
            wget("b5", 1),
            wget("s1", 0),
            wget("s1", 1),
            wget("s2", 0),
            wget("s2", 1),
            wget("s3", 0),
            wget("s3", 1),
            wget("s4", 0),
            wget("s4", 1),
            wget("s5", 0),
            wget("s5", 1),
        )
        try:
            total += store.insert_rows(
                "advanced_pankou",
                [row],
                [
                    "date",
                    "stock_code",
                    "buy1_price",
                    "buy1_volume",
                    "buy2_price",
                    "buy2_volume",
                    "buy3_price",
                    "buy3_volume",
                    "buy4_price",
                    "buy4_volume",
                    "buy5_price",
                    "buy5_volume",
                    "sell1_price",
                    "sell1_volume",
                    "sell2_price",
                    "sell2_volume",
                    "sell3_price",
                    "sell3_volume",
                    "sell4_price",
                    "sell4_volume",
                    "sell5_price",
                    "sell5_volume",
                ],
                replace_on=["date", "stock_code"],
            )
        except Exception as exc:
            logger.debug("pankou insert error for %s: %s", code, exc)
            row2 = (date, code, json.dumps(data, ensure_ascii=False))
            total += store.insert_rows(
                "advanced_pankou",
                [row2],
                ["date", "stock_code", "raw_json"],
                replace_on=["date", "stock_code"],
            )
    if total:
        store.log_collect("advanced_pankou", "/advanced/pankou", total, "ok")
    return total


def collect_advanced_chouma(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes[:100]:
        data = client.get("/advanced/chouma", {"code": code, "date": date})
        rows = []
        for item in _items(data, "chips"):
            if not isinstance(item, dict):
                continue
            rows.append(
                (
                    date,
                    code,
                    _to_float(_field(item, "price_level", "price", default=0)),
                    _to_int(_field(item, "shares", "volume", default=0)),
                    str(_field(item, "chouma_type", "type", default="")),
                )
            )
        if rows:
            total += store.insert_rows(
                "advanced_chouma",
                rows,
                ["date", "stock_code", "price_level", "shares", "chouma_type"],
                replace_on=["date", "stock_code", "price_level", "chouma_type"],
            )
    if total:
        store.log_collect("advanced_chouma", "/advanced/chouma", total, "ok")
    return total


def collect_advanced_main_monitor(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes[:100]:
        data = client.get("/advanced/main-monitor", {"code": code})
        rows = []
        for index, item in enumerate(_items(data, "monitors"), start=1):
            if isinstance(item, dict):
                row_stock_code = str(_field(item, "stock_code", "code", default=code))
                rows.append(
                    (
                        date,
                        row_stock_code,
                        str(_field(item, "stock_name", "name", default="")),
                        _to_int(_field(item, "main_net_inflow", "main_fund_net", "net_inflow", default=0)),
                        _to_int(_field(item, "ranking", "rank", default=index)),
                    )
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 4:
                rows.append((date, code, "", _to_int(item[3]), index))
        if rows:
            total += store.insert_rows(
                "advanced_main_monitor",
                rows,
                ["date", "stock_code", "stock_name", "main_net_inflow", "ranking"],
                replace_on=["date", "stock_code", "ranking"],
            )
    if total:
        store.log_collect("advanced_main_monitor", "/advanced/main-monitor", total, "ok")
    return total


def collect_advanced_zjmm_min(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """Collect intraday main-money minute evidence."""
    total = 0
    for code in stock_codes[:50]:
        data = client.get("/advanced/zjmm-min", {"code": code, "date": date})
        rows = []
        for item in _items(data, "minutes", "points"):
            if isinstance(item, dict):
                if not _matches_trade_date(item, date):
                    continue
                rows.append(
                    (
                        date,
                        code,
                        str(_field(item, "time", "t", "timestamp", default="")),
                        _to_int(_field(item, "main_net_inflow", "main_net", "main_fund_net", "net_amount", default=0)),
                        _to_int(_field(item, "super_net_inflow", "super_net", "super_amount", default=0)),
                        _to_int(_field(item, "big_net_inflow", "big_net", "big_amount", "big_net_amount", default=0)),
                    )
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                rows.append((date, code, str(item[0]), _to_int(item[1]), 0, 0))
        if rows:
            total += store.insert_rows(
                "advanced_zjmm_min",
                rows,
                ["date", "stock_code", "time", "main_net_inflow", "super_net_inflow", "big_net_inflow"],
                replace_on=["date", "stock_code", "time"],
            )
    if total:
        store.log_collect("advanced_zjmm_min", "/advanced/zjmm-min", total, "ok")
    return total


def collect_advanced_dadan_kline(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """Collect large-order K-line evidence as one aggregate row per stock/date/ktype."""
    total = 0
    for code in stock_codes[:50]:
        data = client.get("/advanced/dadan-kline", {"code": code})
        grouped: dict[str, int] = {}
        for item in _items(data, "klines", "points"):
            if not isinstance(item, dict) or not _matches_trade_date(item, date):
                continue
            ktype = str(_field(item, "ktype", "type", "period", default="default"))
            amount = _to_int(
                _field(item, "big_net_amount", "big_order_net", "dadan_net", "net_amount", "amount", default=0)
            )
            grouped[ktype] = grouped.get(ktype, 0) + amount
        rows = [(date, code, amount, ktype) for ktype, amount in grouped.items()]
        if rows:
            total += store.insert_rows(
                "advanced_dadan_kline",
                rows,
                ["date", "stock_code", "big_net_amount", "ktype"],
                replace_on=["date", "stock_code", "ktype"],
            )
    if total:
        store.log_collect("advanced_dadan_kline", "/advanced/dadan-kline", total, "ok")
    return total


def collect_advanced_main_activity_kline(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """Collect main-activity K-line evidence as one aggregate row per stock/date/ktype."""
    total = 0
    for code in stock_codes[:50]:
        data = client.get("/advanced/main-activity-kline", {"code": code})
        grouped: dict[str, list[float]] = {}
        top_ktype = data.get("ktype") if isinstance(data, dict) else None
        for item in _items(data, "klines", "points"):
            if isinstance(item, dict):
                if not _matches_trade_date(item, date):
                    continue
                ktype = str(_field(item, "ktype", "type", "period", default=top_ktype or "default"))
                score = _to_float(_field(item, "main_activity_score", "activity_score", "score", "value", default=0))
                grouped.setdefault(ktype, []).append(score)
            elif isinstance(item, (int, float)):
                grouped.setdefault(str(top_ktype or "default"), [_to_float(item)])
            elif isinstance(item, (list, tuple)) and item:
                grouped.setdefault(str(top_ktype or "default"), [_to_float(item[-1])])
        rows = [(date, code, max(scores) if scores else 0.0, ktype) for ktype, scores in grouped.items()]
        if rows:
            total += store.insert_rows(
                "advanced_main_activity_kline",
                rows,
                ["date", "stock_code", "main_activity_score", "ktype"],
                replace_on=["date", "stock_code", "ktype"],
            )
    if total:
        store.log_collect("advanced_main_activity_kline", "/advanced/main-activity-kline", total, "ok")
    return total


def collect_advanced_kline_extras(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """Collect existing advanced K-line endpoints used by historical reports."""
    total = 0
    kline_endpoints = [
        (
            "/advanced/dadan-kline-today",
            "advanced_dadan_kline_today",
            ["date", "stock_code", "big_net_amount", "ktype"],
            ["big_net_amount", "big_order_net", "net_amount"],
        ),
        (
            "/advanced/kline-today",
            "advanced_kline_today",
            ["date", "stock_code", "open", "high", "low", "close", "volume", "turnover", "ktype"],
            ["open", "high", "low", "close", "volume", "turnover", "ktype"],
        ),
        (
            "/advanced/kline-today-tyd",
            "advanced_kline_today_tyd",
            ["date", "stock_code", "tuo_amount", "ya_amount", "ktype"],
            ["tuo_amount", "ya_amount", "ktype"],
        ),
        (
            "/advanced/kline-today-dadan-new",
            "advanced_kline_today_dadan_new",
            ["date", "stock_code", "big_net_amount", "ktype"],
            ["big_net_amount", "big_order_net", "net_amount"],
        ),
        (
            "/advanced/kline-today-main-activity",
            "advanced_kline_today_main_activity",
            ["date", "stock_code", "main_activity_score", "ktype"],
            ["main_activity_score", "activity_score", "score"],
        ),
        (
            "/advanced/kline-today-duidao",
            "advanced_kline_today_duidao",
            ["date", "stock_code", "duidao_amount", "ktype"],
            ["duidao_amount", "amount", "net_amount"],
        ),
    ]

    for code in stock_codes[:50]:
        for endpoint, table, cols, value_fields in kline_endpoints:
            data = client.get(endpoint, {"code": code, "ktype": "d"})
            rows = []
            for item in _items(data, "klines", "points"):
                if not isinstance(item, dict) or not _matches_trade_date(item, date):
                    continue
                row_data = [date, code]
                for col in cols[2:]:
                    if col == "ktype":
                        row_data.append(str(_field(item, "ktype", "type", "period", default="d")))
                    else:
                        row_data.append(_field(item, col, *value_fields, default=0))
                rows.append(tuple(row_data))
            if rows:
                total += store.insert_rows(
                    table,
                    rows,
                    cols,
                    replace_on=["date", "stock_code", "ktype"] if "ktype" in cols else ["date", "stock_code"],
                )
    if total:
        store.log_collect("advanced_kline_extras", "/advanced/kline-*", total, "ok")
    return total


def collect_all_advanced_stock(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> dict:
    results = {}
    active = stock_codes[:100]
    results["advanced_pankou"] = collect_advanced_pankou(client, store, date, active)
    results["advanced_chouma"] = collect_advanced_chouma(client, store, date, active)
    results["advanced_main_monitor"] = collect_advanced_main_monitor(client, store, date, active)
    results["advanced_zjmm_min"] = collect_advanced_zjmm_min(client, store, date, active)
    results["advanced_dadan_kline"] = collect_advanced_dadan_kline(client, store, date, active)
    results["advanced_main_activity_kline"] = collect_advanced_main_activity_kline(client, store, date, active)
    results["advanced_kline_extras"] = collect_advanced_kline_extras(client, store, date, active)
    return results
