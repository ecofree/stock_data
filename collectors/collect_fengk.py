"""Fengk (review/replay) data collectors (3 endpoints)."""
from base import KPLClient, DuckDBStore


def collect_fengk_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    total = 0
    page_size = 100
    for idx in range(0, 1000, page_size):
        data = client.get("/fengk/list", {"index": str(idx), "page_size": str(page_size), "date": date})
        if not data:
            break
        items = data.get("List", data.get("data", data.get("list", [])))
        day = data.get("Day", date)
        if not items:
            break
        rows = []
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) >= 4:
                rows.append((
                    day,
                    str(item[0]),       # stock_code
                    str(item[1]),       # stock_name
                    item[3] if len(item) > 3 else 0,  # change_pct
                    item[5] if len(item) > 5 else 0,  # turnover
                    item[4] if len(item) > 4 else 0,  # market_cap
                    item[6] if len(item) > 6 else 0,  # main_net
                    str(item[8]) if len(item) > 8 else "",  # reason
                ))
            elif isinstance(item, dict):
                rows.append((
                    day,
                    str(item.get("code", item.get("stock_code", ""))),
                    item.get("name", item.get("stock_name", "")),
                    item.get("change_pct", item.get("涨跌幅", 0)),
                    item.get("turnover", item.get("成交额", 0)),
                    item.get("market_cap", item.get("市值", 0)),
                    item.get("main_net", item.get("主力净额", 0)),
                    str(item.get("reason", item.get("原因", ""))),
                ))
        if rows:
            n = store.insert_rows("fengk_list", rows,
                ["date", "stock_code", "stock_name", "change_pct", "turnover",
                 "market_cap", "main_net", "reason"])
            total += n
        if len(items) < page_size:
            break
    if total:
        store.log_collect("fengk_list", "/fengk/list", total, "ok")
    return total


def collect_fengk_yd_plate(client: KPLClient, store: DuckDBStore, date: str) -> list:
    data = client.get("/fengk/yd-plate", {"date": date})
    if not data:
        return []
    items = data if isinstance(data, list) else data.get("data", data.get("plates", []))
    plate_names = []
    rows = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name", item.get("plate_name", ""))
            plate_names.append(name)
            rows.append((
                date, name,
                item.get("main_net", item.get("主力净额", 0)),
                item.get("change_pct", item.get("涨跌幅", 0)),
            ))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name = str(item[0]) if isinstance(item[0], str) else str(item[1])
            plate_names.append(name)
            rows.append((date, name, item[2] if len(item) > 2 else 0,
                         item[3] if len(item) > 3 else 0))
    if rows:
        n = store.insert_rows("fengk_yd_plate", rows,
            ["date", "sector_name", "main_net", "change_pct"])
        store.log_collect("fengk_yd_plate", "/fengk/yd-plate", n, "ok")
    return plate_names


def collect_fengk_yd_plate_info(client: KPLClient, store: DuckDBStore, date: str, plate_names: list) -> int:
    total = 0
    for plate in plate_names[:20]:
        data = client.get("/fengk/yd-plate-info", {"plate": plate, "date": date})
        if not data:
            continue
        stocks = data if isinstance(data, list) else data.get("data", data.get("stocks", []))
        rows = []
        for s in stocks:
            if isinstance(s, dict):
                rows.append((
                    date, plate,
                    str(s.get("stock_code", s.get("code", ""))),
                    s.get("stock_name", s.get("name", "")),
                    s.get("change_pct", s.get("涨跌幅", 0)),
                ))
        if rows:
            n = store.insert_rows("fengk_yd_plate_info", rows,
                ["date", "sector_name", "stock_code", "stock_name", "change_pct"])
            total += n
    if total:
        store.log_collect("fengk_yd_plate_info", "/fengk/yd-plate-info", total, "ok")
    return total


def collect_all_fengk(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    results = {}
    results["fengk_list"] = collect_fengk_list(client, store, date)
    plates = collect_fengk_yd_plate(client, store, date)
    results["fengk_yd_plate"] = len(plates)
    results["fengk_yd_plate_info"] = collect_fengk_yd_plate_info(client, store, date, plates)
    return results
