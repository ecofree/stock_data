"""L2 data collectors (11 endpoints)."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore, logger


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _intraday_main_net(point: dict) -> float:
    return _to_float(
        point.get(
            "main_net_inflow",
            point.get("main_fund_net", point.get("主力净额", 0)),
        )
    )


def _extract_index_rows(data, date: str) -> list[tuple[str, str, str, float, float]]:
    if not data:
        return []
    if isinstance(data, dict):
        indices = data.get("indexes")
        if indices is None:
            indices = data.get("data", data.get("indices", []))
    elif isinstance(data, list):
        indices = data
    else:
        return []

    rows = []
    for idx in indices or []:
        if not isinstance(idx, dict):
            continue
        code = str(idx.get("index_code") or idx.get("stock_id") or idx.get("code") or "")
        if not code:
            continue
        rows.append(
            (
                date,
                code,
                idx.get("index_name") or idx.get("name") or "",
                _to_float(idx.get("price", idx.get("value", idx.get("价格", 0)))),
                _to_float(idx.get("change_pct", idx.get("涨跌幅", 0))),
            )
        )
    return rows


def _items(data, *keys: str) -> list:
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys + ("data", "items", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if any(key in data for key in ("time", "price", "volume")):
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
    value = _field(item, "date", "trade_date", "day", "dt", "datetime", default=None)
    if value is None:
        return True
    compact = _compact_date(value)
    return len(compact) < 8 or compact == _compact_date(date)


def _response_matches_trade_date(data, date: str) -> bool:
    return not isinstance(data, dict) or _matches_trade_date(data, date)


def collect_l2_stock_intraday(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/l2/stock-intraday", {"code": code, "date": date})
        if not data or not _response_matches_trade_date(data, date):
            continue
        intraday = data if isinstance(data, list) else data.get("data", data.get("intraday", []))
        rows = []
        for pt in intraday:
            if isinstance(pt, dict):
                if not _matches_trade_date(pt, date):
                    continue
                rows.append((
                    date, code,
                    str(pt.get("time", pt.get("t", ""))),
                    pt.get("price", pt.get("p", 0)),
                    pt.get("avg_price", pt.get("avg", 0)),
                    pt.get("volume", pt.get("v", 0)),
                    pt.get("turnover", pt.get("amount", 0)),
                    _intraday_main_net(pt),
                ))
            elif isinstance(pt, (list, tuple)) and len(pt) >= 4:
                rows.append((
                    date, code, str(pt[0]), pt[1], pt[2], pt[3],
                    pt[4] if len(pt) > 4 else 0,
                    pt[5] if len(pt) > 5 else 0,
                ))
        if rows:
            n = store.insert_rows("l2_stock_intraday", rows,
                ["date", "stock_code", "time", "price", "avg_price", "volume", "turnover", "main_fund_net"])
            total += n
    if total:
        store.log_collect("l2_stock_intraday", "/l2/stock-intraday", total, "ok")
    return total


def collect_l2_stock_bigorder(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """API returns {stock_code, date, big_order_buy_total, big_order_sell_total, data: [{time, big_order_net, ...}]}."""
    total = 0
    for code in stock_codes:
        data = client.get("/l2/stock-bigorder", {"code": code, "date": date})
        if not data or not _response_matches_trade_date(data, date):
            continue
        # The API response is a dict with "data" as a list of time-series entries
        if isinstance(data, dict):
            orders = data.get("data", [])
            api_date = date
        elif isinstance(data, list):
            orders = data
            api_date = date
        else:
            continue
        rows = []
        for o in orders:
            if isinstance(o, dict):
                if not _matches_trade_date(o, date):
                    continue
                rows.append((
                    api_date, code,
                    str(o.get("time", o.get("t", ""))),
                    o.get("big_order_net", o.get("大单净额", 0)),
                    o.get("intraday_buy", o.get("大单买入", 0)),
                    o.get("intraday_sell", o.get("大单卖出", 0)),
                ))
        if rows:
            n = store.insert_rows("l2_stock_bigorder", rows,
                ["date", "stock_code", "time", "big_net_amount", "big_buy", "big_sell"])
            total += n
    if total:
        store.log_collect("l2_stock_bigorder", "/l2/stock-bigorder", total, "ok")
    return total


def collect_l2_sector_intraday(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:50]:
        data = client.get("/l2/sector-intraday", {"code": code, "date": date})
        if not data or not _response_matches_trade_date(data, date):
            continue
        intraday = data if isinstance(data, list) else data.get("data", data.get("intraday", []))
        rows = []
        for pt in intraday:
            if isinstance(pt, dict):
                if not _matches_trade_date(pt, date):
                    continue
                rows.append((
                    date, code,
                    str(pt.get("time", pt.get("t", ""))),
                    pt.get("price", pt.get("p", 0)),
                    pt.get("volume", pt.get("v", 0)),
                    pt.get("turnover", pt.get("amount", 0)),
                ))
        if rows:
            n = store.insert_rows("l2_sector_intraday", rows,
                ["date", "sector_code", "time", "price", "volume", "turnover"])
            total += n
    if total:
        store.log_collect("l2_sector_intraday", "/l2/sector-intraday", total, "ok")
    return total


def collect_l2_realtime_all_boards(client: KPLClient, store: DuckDBStore, date: str) -> int:
    """API returns {first_board: [...], second_board: [...], ...} - each key is the board type."""
    data = client.get("/l2/realtime/all-boards")
    if not data:
        return 0
    total = 0
    # Map board type keys to level integers
    board_key_to_level = {
        "first_board": 1, "second_board": 2, "third_board": 3,
        "fourth_board": 4, "fifth_board": 5, "gouban": 6,
    }
    for key, stocks in data.items():
        if not isinstance(stocks, list):
            continue
        board_level = board_key_to_level.get(key, 0)
        rows = []
        for s in stocks:
            if isinstance(s, dict):
                rows.append((
                    date,
                    s.get("board_type", board_level),
                    str(s.get("stock_code", s.get("code", ""))),
                    s.get("stock_name", s.get("name", "")),
                    str(s.get("timestamp", s.get("limit_up_time", ""))),
                ))
            elif isinstance(s, (list, tuple)) and len(s) >= 3:
                rows.append((date, board_level, str(s[0]), str(s[1]), str(s[2]) if len(s) > 2 else ""))
        if rows:
            n = store.insert_rows("l2_realtime_all_boards", rows,
                ["date", "board_level", "stock_code", "stock_name", "limit_up_time"])
            total += n
    if total:
        store.log_collect("l2_realtime_all_boards", "/l2/realtime/all-boards", total, "ok")
        return total
    store.insert_raw("/l2/realtime/all-boards", data)
    return 0


def collect_l2_realtime_index_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/l2/realtime/index-list")
    if not data:
        return 0
    rows = _extract_index_rows(data, date)
    if rows:
        n = store.insert_rows("l2_realtime_index_list", rows,
            ["date", "index_code", "index_name", "price", "change_pct"])
        store.log_collect("l2_realtime_index_list", "/l2/realtime/index-list", n, "ok")
        return n
    store.insert_raw("/l2/realtime/index-list", data)
    return 0


def collect_l2_realtime_index_trend(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/l2/realtime/index-trend")
    rows = []
    for item in _items(data, "trend", "points"):
        if not isinstance(item, dict):
            continue
        rows.append(
            (
                date,
                str(_field(item, "index_code", "stock_id", "code", default="SH000001")),
                str(_field(item, "time", "t", "timestamp", default="")),
                _to_float(_field(item, "price", "value", "p", default=0)),
                int(_to_float(_field(item, "volume", "v", default=0))),
            )
        )
    if rows:
        n = store.insert_rows(
            "l2_realtime_index_trend",
            rows,
            ["date", "index_code", "time", "price", "volume"],
            replace_on=["date", "index_code", "time"],
        )
        store.log_collect("l2_realtime_index_trend", "/l2/realtime/index-trend", n, "ok")
        return n
    if data:
        store.insert_raw("/l2/realtime/index-trend", data)
    return 0


def collect_l2_sector_volume(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:30]:
        data = client.get("/l2/sector-volume", {"code": code})
        if not _response_matches_trade_date(data, date):
            continue
        rows = []
        for item in _items(data, "points", "volumes"):
            if not isinstance(item, dict):
                continue
            if not _matches_trade_date(item, date):
                continue
            rows.append(
                (
                    date,
                    code,
                    str(_field(item, "time", "t", "timestamp", default="")),
                    int(_to_float(_field(item, "volume", "v", default=0))),
                    int(_to_float(_field(item, "turnover", "amount", default=0))),
                )
            )
        if rows:
            total += store.insert_rows(
                "l2_sector_volume",
                rows,
                ["date", "sector_code", "time", "volume", "turnover"],
                replace_on=["date", "sector_code", "time"],
            )
    if total:
        store.log_collect("l2_sector_volume", "/l2/sector-volume", total, "ok")
    return total


def _collect_l2_tick_like(
    client: KPLClient,
    store: DuckDBStore,
    *,
    endpoint: str,
    table_name: str,
    date: str,
    stock_codes: list,
    direction_column: str,
) -> int:
    total = 0
    for code in stock_codes[:30]:
        data = client.get(endpoint, {"code": code, "date": date})
        rows = []
        for item in _items(data, "ticks", "orders", "history"):
            if isinstance(item, dict):
                rows.append(
                    (
                        date,
                        code,
                        str(_field(item, "time", "t", "timestamp", default="")),
                        _to_float(_field(item, "price", "p", default=0)),
                        int(_to_float(_field(item, "volume", "v", default=0))),
                        str(
                            _field(
                                item,
                                direction_column,
                                "direction",
                                "order_type",
                                "side",
                                "type",
                                default="",
                            )
                        ),
                    )
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                rows.append((date, code, str(item[0]), item[1], item[2], str(item[3]) if len(item) > 3 else ""))
        if rows:
            total += store.insert_rows(
                table_name,
                rows,
                ["date", "stock_code", "time", "price", "volume", direction_column],
                replace_on=["date", "stock_code", "time"],
            )
    if total:
        store.log_collect(table_name, endpoint, total, "ok")
    return total


def collect_l2_tick_history(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    return _collect_l2_tick_like(
        client,
        store,
        endpoint="/l2/tick-history",
        table_name="l2_tick_history",
        date=date,
        stock_codes=stock_codes,
        direction_column="direction",
    )


def collect_l2_tick_orders(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    return _collect_l2_tick_like(
        client,
        store,
        endpoint="/l2/tick-orders",
        table_name="l2_tick_orders",
        date=date,
        stock_codes=stock_codes,
        direction_column="order_type",
    )


def collect_l2_tick_orders_all(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    return _collect_l2_tick_like(
        client,
        store,
        endpoint="/l2/tick-orders-all",
        table_name="l2_tick_orders_all",
        date=date,
        stock_codes=stock_codes,
        direction_column="order_type",
    )


def collect_all_l2(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list, sector_codes: list) -> dict:
    results = {}
    results["l2_realtime_all_boards"] = collect_l2_realtime_all_boards(client, store, date)
    results["l2_realtime_index_list"] = collect_l2_realtime_index_list(client, store, date)
    results["l2_realtime_index_trend"] = collect_l2_realtime_index_trend(client, store, date)
    # L2 per-stock data (limit to top 100 to avoid too many requests)
    active_stocks = stock_codes[:100]
    results["l2_stock_intraday"] = collect_l2_stock_intraday(client, store, date, active_stocks)
    results["l2_stock_bigorder"] = collect_l2_stock_bigorder(client, store, date, active_stocks)
    # Tick/order endpoints can be large; use a bounded operator sample.
    tick_stocks = stock_codes[:30]
    results["l2_tick_history"] = collect_l2_tick_history(client, store, date, tick_stocks)
    results["l2_tick_orders"] = collect_l2_tick_orders(client, store, date, tick_stocks)
    results["l2_tick_orders_all"] = collect_l2_tick_orders_all(client, store, date, tick_stocks)
    # L2 per-sector data
    results["l2_sector_intraday"] = collect_l2_sector_intraday(client, store, date, sector_codes)
    results["l2_sector_volume"] = collect_l2_sector_volume(client, store, date, sector_codes)
    return results
