"""Dragon Tiger Board (LHB) collectors (7 endpoints)."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore, logger


def collect_lhb_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/lhb/list", {"date": date})
    if not data:
        return 0
    stocks = data.get("stocks", data.get("data", []))
    rows = []
    for s in stocks:
        if isinstance(s, dict):
            rows.append((
                data.get("date", date),
                str(s.get("stock_code", s.get("code", s.get("股票代码", "")))),
                s.get("stock_name", s.get("name", s.get("股票名称", ""))),
                s.get("change_pct", s.get("涨跌幅", 0)),
                s.get("turnover", s.get("成交额", 0)),
                s.get("reason", s.get("上榜原因", "")),
                s.get("buy_amount", s.get("买入额", 0)),
                s.get("sell_amount", s.get("卖出额", 0)),
                s.get("net_amount", s.get("净买入", 0)),
            ))
    if rows:
        n = store.insert_rows("lhb_list", rows,
            ["date", "stock_code", "stock_name", "change_pct", "turnover",
             "reason", "buy_amount", "sell_amount", "net_amount"],
            replace_on=["date", "stock_code"])
        store.log_collect("lhb_list", "/lhb/list", n, "ok")
        return n
    store.insert_raw("/lhb/list", data)
    return 0


def collect_lhb_detail(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/lhb/detail", {"code": code, "date": date})
        if not data:
            continue
        rows = []
        # Current payload shape: one entry per brokerage seat under
        # "businesses" carrying both buy and sell amounts.
        businesses = data.get("businesses", [])
        if isinstance(businesses, list) and businesses:
            for seat in businesses:
                if isinstance(seat, dict):
                    buy = seat.get("buy", 0) or 0
                    sell = seat.get("sell", 0) or 0
                    net = seat.get("net", buy - sell)
                    rows.append((
                        date, code,
                        seat.get("name", seat.get("broker_name", "")),
                        buy, sell, net, (net or 0) >= 0,
                    ))
        else:
            # Legacy payload shape: separate buy/sell seat lists.
            for side_key, is_buy in [("buy", True), ("sell", False),
                                      ("买入席位", True), ("卖出席位", False)]:
                seats = data.get(side_key, [])
                if isinstance(seats, list):
                    for seat in seats:
                        if isinstance(seat, dict):
                            rows.append((
                                date, code,
                                seat.get("broker_name", seat.get("营业部", "")),
                                seat.get("buy_amount", seat.get("买入额", 0)),
                                seat.get("sell_amount", seat.get("卖出额", 0)),
                                seat.get("net_amount", seat.get("净额", 0)),
                                is_buy,
                            ))
        if rows:
            n = store.insert_rows("lhb_detail", rows,
                ["date", "stock_code", "broker_name", "buy_amount",
                 "sell_amount", "net_amount", "is_buy"],
                replace_on=["date", "stock_code", "broker_name"])
            total += n
    if total:
        store.log_collect("lhb_detail", "/lhb/detail", total, "ok")
    return total


def collect_lhb_dataframe(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/lhb/dataframe", {"date": date})
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("lhb_dataframe", [row], ["date", "raw_json"])
    store.log_collect("lhb_dataframe", "/lhb/dataframe", n, "ok")
    return n


def collect_lhb_update_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/lhb/update-list")
    if not data:
        return 0
    items = data if isinstance(data, list) else data.get("data", data.get("stocks", []))
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                str(item.get("stock_code", item.get("code", ""))),
                item.get("stock_name", item.get("name", "")),
            ))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            rows.append((date, str(item[0]), str(item[1])))
    if rows:
        n = store.insert_rows("lhb_update_list", rows, ["date", "stock_code", "stock_name"])
        store.log_collect("lhb_update_list", "/lhb/update-list", n, "ok")
        return n
    store.insert_raw("/lhb/update-list", data)
    return 0


def collect_lhb_raw_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/lhb/raw-list", {"date": date})
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("lhb_raw_list", [row], ["date", "raw_json"])
    store.log_collect("lhb_raw_list", "/lhb/raw-list", n, "ok")
    return n


def collect_lhb_youzi(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/lhb/youzi-dongxiang", {"date": date})
    if not data:
        return 0
    dongxiang = data.get("DongXiang", data.get("data", []))
    rows = []
    for item in dongxiang:
        if isinstance(item, dict):
            rows.append((
                date,
                item.get("broker_name", item.get("营业部", "")),
                str(item.get("stock_code", item.get("股票代码", ""))),
                item.get("stock_name", item.get("股票名称", "")),
                item.get("buy_amount", item.get("买入额", 0)),
                item.get("sell_amount", item.get("卖出额", 0)),
            ))
    if rows:
        n = store.insert_rows("lhb_youzi_dongxiang", rows,
            ["date", "broker_name", "stock_code", "stock_name", "buy_amount", "sell_amount"])
        store.log_collect("lhb_youzi_dongxiang", "/lhb/youzi-dongxiang", n, "ok")
        return n
    store.insert_raw("/lhb/youzi-dongxiang", data)
    return 0


def collect_lhb_top_title(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/lhb/top-title")
    if not data:
        return 0
    titles = data if isinstance(data, list) else data.get("data", data.get("titles", []))
    rows = []
    for t in titles:
        if isinstance(t, str):
            rows.append((date, t))
        elif isinstance(t, dict):
            rows.append((date, t.get("title", t.get("name", ""))))
    if rows:
        n = store.insert_rows("lhb_top_title", rows, ["date", "title"])
        store.log_collect("lhb_top_title", "/lhb/top-title", n, "ok")
        return n
    store.insert_raw("/lhb/top-title", data)
    return 0


def collect_all_lhb(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    results = {}
    results["lhb_list"] = collect_lhb_list(client, store, date)
    results["lhb_dataframe"] = collect_lhb_dataframe(client, store, date)
    results["lhb_update_list"] = collect_lhb_update_list(client, store, date)
    results["lhb_raw_list"] = collect_lhb_raw_list(client, store, date)
    results["lhb_youzi"] = collect_lhb_youzi(client, store, date)
    results["lhb_top_title"] = collect_lhb_top_title(client, store, date)
    # lhb_detail needs stock codes from lhb_list
    lhb_data = client.get("/lhb/list", {"date": date})
    if lhb_data and isinstance(lhb_data, dict):
        codes = [str(s.get("stock_code", s.get("code", "")))
                 for s in lhb_data.get("stocks", []) if isinstance(s, dict)]
        codes = [c for c in codes if c]
        results["lhb_detail"] = collect_lhb_detail(client, store, date, codes[:30])
    else:
        results["lhb_detail"] = 0
    return results
