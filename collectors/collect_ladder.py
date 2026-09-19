"""Ladder (board ladder) data collectors (7 endpoints)."""
from datetime import datetime
from trade_system.data_store import KPLClient, DuckDBStore


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
             "consecutive_days", "consecutive_count", "is_first_board"],
            replace_on=["date", "board_level", "stock_code"])
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
            ["date", "stock_code", "stock_name", "board_count"],
            replace_on=["date", "stock_code"])
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
            ["date", "sector_code", "sector_name", "board_count"],
            replace_on=["date", "sector_code"])
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
            ["date", "stock_code", "stock_name", "broken_time", "max_price"],
            replace_on=["date", "stock_code", "broken_time"])
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
            ["date", "stock_code", "stock_name", "withdrawal_pct"],
            replace_on=["date", "stock_code"])
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
                ["date", "board_type", "stock_code", "stock_name"],
                replace_on=["date", "board_type", "stock_code"])
            total += n
    if total:
        store.log_collect("ladder_board_stocks", "/ladder/board-stocks", total, "ok")
    return total


def _collect_realtime_boards(client, store, date, *, grouped=False):
    """Both KPL products share validation/publication, but retain separate raw tables."""
    endpoint = "/l2/realtime/all-boards" if grouped else "/ladder/realtime-boards"
    table = "l2_realtime_all_boards" if grouped else "ladder_realtime_boards"
    data = client.get(endpoint)
    if not data:
        return 0
    levels = {"first_board": 1, "second_board": 2, "third_board": 3, "fourth_board": 4, "fifth_board": 5}
    if not isinstance(data, (dict, list)):
        store.insert_raw(endpoint, data)
        return 0
    received = datetime.now()
    groups = data.items() if grouped and isinstance(data, dict) else [(None, data if isinstance(data, list) else data.get("data", data.get("stocks", [])))]
    rows, codes = [], set()
    try:
        if isinstance(data, dict) and str(data.get("trade_date", data.get("date", date))).replace("-", "") != date.replace("-", ""):
            raise ValueError("realtime board date mismatch")
        for group, stocks in groups:
            if not isinstance(stocks, list):
                if group in levels or group == "gouban":
                    raise ValueError("invalid realtime board group")
                continue
            for item in stocks:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    item = dict(code=item[0], name=item[1], time=item[2])
                code = str(item.get("stock_code", item.get("code", "")))
                height = item.get("board_type", item.get("level", levels.get(group)))
                if (isinstance(height, bool) or not str(height).isascii() or not str(height).isdigit()
                        or int(height) < 1 or len(code) != 6 or not code.isascii() or not code.isdigit()
                        or code in codes or str(item.get("trade_date", item.get("date", date))).replace("-", "") != date.replace("-", "")):
                    raise ValueError("unqualified realtime board identity, date or height")
                codes.add(code)
                rows.append((date, int(height), code, item.get("stock_name", item.get("name", "")),
                             str(item.get("timestamp", item.get("limit_up_time", item.get("time", "")))), received))
    except (AttributeError, TypeError, ValueError):
        store.insert_raw(endpoint, data)
        return 0
    if not rows:
        store.insert_raw(endpoint, data)
        return 0
    # Validate the entire response before the existing atomic batch writer sees it.
    count = store.insert_rows(table, rows,
        ["date", "board_level" if grouped else "board_type", "stock_code", "stock_name", "limit_up_time", "fetched_at"],
        replace_on=["date", "stock_code"])
    store.log_collect(table, endpoint, count, "ok")
    return count


def collect_ladder_realtime_boards(client: KPLClient, store: DuckDBStore, date: str) -> int:
    return _collect_realtime_boards(client, store, date)


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
