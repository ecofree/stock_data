"""Ladder (board ladder) data collectors (7 endpoints)."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore, logger


def collect_ladder_market(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/ladder/market", {"date": date})
    if not data:
        return 0
    ladder = data.get("ladder", {})
    rows = []
    for level_str, stocks in ladder.items():
        level = int(level_str) if str(level_str).isdigit() else 0
        if not isinstance(stocks, list):
            continue
        for s in stocks:
            if isinstance(s, dict):
                rows.append((
                    data.get("date", date), level,
                    str(s.get("stock_code", s.get("code", ""))),
                    s.get("stock_name", s.get("name", "")),
                    s.get("consecutive_days", level),
                    s.get("consecutive_count", s.get("board_count", 0)),
                    level == 1,
                ))
    if rows:
        n = store.insert_rows("ladder_market", rows,
            ["date", "board_level", "stock_code", "stock_name",
             "consecutive_days", "consecutive_count", "is_first_board"])
        store.log_collect("ladder_market", "/ladder/market", n, "ok")
        return n
    store.insert_raw("/ladder/market", data)
    return 0


def collect_ladder_consecutive(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/ladder/consecutive", {"date": date})
    if not data:
        return 0
    rows = []
    if isinstance(data, dict):
        resp_date = data.get("date", date)
        ladder = data.get("ladder", {})
        if not isinstance(ladder, dict):
            ladder = {}
        for group, stocks in ladder.items():
            board_level = int(group) if str(group).isdigit() else 0
            if isinstance(stocks, list):
                for s in stocks:
                    if isinstance(s, dict):
                        rows.append((
                            resp_date,
                            str(s.get("stock_code", s.get("code", ""))),
                            s.get("stock_name", s.get("name", "")),
                            s.get("board_count", board_level),
                        ))
    if rows:
        n = store.insert_rows("ladder_consecutive", rows,
            ["date", "stock_code", "stock_name", "board_count"])
        store.log_collect("ladder_consecutive", "/ladder/consecutive", n, "ok")
        return n
    store.insert_raw("/ladder/consecutive", data)
    return 0


def collect_ladder_sector(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/ladder/sector", {"date": date})
    if not data:
        return 0
    sectors = data if isinstance(data, list) else data.get("data", data.get("sectors", []))
    rows = []
    for s in sectors:
        if isinstance(s, dict):
            rows.append((
                date,
                str(s.get("sector_code", s.get("code", ""))),
                s.get("sector_name", s.get("name", "")),
                s.get("board_count", s.get("count", 0)),
            ))
    if rows:
        n = store.insert_rows("ladder_sector", rows,
            ["date", "sector_code", "sector_name", "board_count"])
        store.log_collect("ladder_sector", "/ladder/sector", n, "ok")
        return n
    store.insert_raw("/ladder/sector", data)
    return 0


def collect_ladder_broken(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/ladder/broken", {"date": date})
    if not data:
        return 0
    stocks = data if isinstance(data, list) else data.get("data", data.get("stocks", []))
    rows = []
    for s in stocks:
        if isinstance(s, dict):
            rows.append((
                date,
                str(s.get("stock_code", s.get("code", ""))),
                s.get("stock_name", s.get("name", "")),
                str(s.get("broken_time", s.get("time", ""))),
                s.get("max_price", s.get("price", 0)),
            ))
    if rows:
        n = store.insert_rows("ladder_broken", rows,
            ["date", "stock_code", "stock_name", "broken_time", "max_price"])
        store.log_collect("ladder_broken", "/ladder/broken", n, "ok")
        return n
    store.insert_raw("/ladder/broken", data)
    return 0


def collect_ladder_sharp_withdrawal(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/ladder/sharp-withdrawal", {"date": date})
    if not data:
        return 0
    stocks = data if isinstance(data, list) else data.get("data", data.get("stocks", []))
    rows = []
    for s in stocks:
        if isinstance(s, dict):
            rows.append((
                date,
                str(s.get("stock_code", s.get("code", ""))),
                s.get("stock_name", s.get("name", "")),
                s.get("withdrawal_pct", s.get("pct", 0)),
            ))
    if rows:
        n = store.insert_rows("ladder_sharp_withdrawal", rows,
            ["date", "stock_code", "stock_name", "withdrawal_pct"])
        store.log_collect("ladder_sharp_withdrawal", "/ladder/sharp-withdrawal", n, "ok")
        return n
    store.insert_raw("/ladder/sharp-withdrawal", data)
    return 0


def collect_ladder_board_stocks(client: KPLClient, store: DuckDBStore, date: str) -> int:
    total = 0
    for bt in ["", "1", "2", "3"]:
        params = {"board_type": bt} if bt else {}
        data = client.get("/ladder/board-stocks", params)
        if not data:
            continue
        stocks = data if isinstance(data, list) else data.get("data", data.get("stocks", []))
        rows = []
        for s in stocks:
            if isinstance(s, dict):
                rows.append((
                    date, bt or "all",
                    str(s.get("stock_code", s.get("code", ""))),
                    s.get("stock_name", s.get("name", "")),
                ))
        if rows:
            n = store.insert_rows("ladder_board_stocks", rows,
                ["date", "board_type", "stock_code", "stock_name"])
            total += n
    if total:
        store.log_collect("ladder_board_stocks", "/ladder/board-stocks", total, "ok")
    return total


def collect_ladder_realtime_boards(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/ladder/realtime-boards")
    if not data:
        return 0
    stocks = data if isinstance(data, list) else data.get("data", data.get("stocks", []))
    rows = []
    for s in stocks:
        if isinstance(s, dict):
            rows.append((
                date,
                str(s.get("board_type", s.get("level", "1"))),
                str(s.get("stock_code", s.get("code", ""))),
                s.get("stock_name", s.get("name", "")),
                str(s.get("limit_up_time", s.get("time", ""))),
            ))
    if rows:
        n = store.insert_rows("ladder_realtime_boards", rows,
            ["date", "board_type", "stock_code", "stock_name", "limit_up_time"])
        store.log_collect("ladder_realtime_boards", "/ladder/realtime-boards", n, "ok")
        return n
    store.insert_raw("/ladder/realtime-boards", data)
    return 0


def collect_all_ladder(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    return {
        "ladder_market": collect_ladder_market(client, store, date),
        "ladder_consecutive": collect_ladder_consecutive(client, store, date),
        "ladder_sector": collect_ladder_sector(client, store, date),
        "ladder_broken": collect_ladder_broken(client, store, date),
        "ladder_sharp_withdrawal": collect_ladder_sharp_withdrawal(client, store, date),
        "ladder_board_stocks": collect_ladder_board_stocks(client, store, date),
        "ladder_realtime_boards": collect_ladder_realtime_boards(client, store, date),
    }
