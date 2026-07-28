"""Dingpan (market monitoring) + Fengk (review) collectors (8 endpoints)."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore, logger


def collect_dingpan_module_versatile(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/dingpan/module-versatile")
    if not data:
        return 0
    total = 0
    skip_keys = {"time", "ttag", "errcode"}
    for module_name, value in data.items():
        if module_name in skip_keys:
            continue
        # Parse structured sub-modules
        if isinstance(value, dict):
            sub_list = value.get("list", value.get("List", []))
            sub_day = value.get("day", value.get("Day", date))
            if isinstance(sub_list, list):
                for item in sub_list:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        # Many modules return [code, name, amount, pct]
                        code = str(item[0])
                        name = str(item[1]) if len(item) > 1 else ""
                        amount = item[2] if len(item) > 2 else 0
                        pct = item[3] if len(item) > 3 else 0
                        if module_name in ("JJZTWM", "WPQC"):
                            row = (sub_day, code, name, amount, pct)
                            n = store.insert_rows("dingpan_weipan", [row],
                                ["date", "stock_code", "stock_name", "amount", "change_pct"])
                            total += n
                    elif isinstance(item, dict):
                        row = (sub_day, module_name, json.dumps(item, ensure_ascii=False))
                        n = store.insert_rows("dingpan_module_versatile", [row],
                            ["date", "module_name", "raw_json"])
                        total += n
            else:
                row = (sub_day, module_name, json.dumps(value, ensure_ascii=False))
                n = store.insert_rows("dingpan_module_versatile", [row],
                    ["date", "module_name", "raw_json"])
                total += n
        else:
            row = (date, module_name, json.dumps(value, ensure_ascii=False))
            n = store.insert_rows("dingpan_module_versatile", [row],
                ["date", "module_name", "raw_json"])
            total += n
    if total:
        store.log_collect("dingpan_module_versatile", "/dingpan/module-versatile", total, "ok")
    return total


def collect_dingpan_northbound_close(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/dingpan/northbound-close-date")
    if not data:
        return 0
    items = data if isinstance(data, list) else data.get("data", [])
    rows = []
    for item in items:
        d = str(item) if not isinstance(item, dict) else item.get("date", str(item))
        rows.append((d,))
    if rows:
        n = store.insert_rows("dingpan_northbound_close_date", rows, ["date"])
        store.log_collect("dingpan_northbound_close_date", "/dingpan/northbound-close-date", n, "ok")
        return n
    return 0


def collect_dingpan_southbound_close(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/dingpan/southbound-close-date")
    if not data:
        return 0
    items = data if isinstance(data, list) else data.get("data", [])
    rows = []
    for item in items:
        d = str(item) if not isinstance(item, dict) else item.get("date", str(item))
        rows.append((d,))
    if rows:
        n = store.insert_rows("dingpan_southbound_close_date", rows, ["date"])
        store.log_collect("dingpan_southbound_close_date", "/dingpan/southbound-close-date", n, "ok")
        return n
    return 0


def collect_dingpan_radar(client: KPLClient, store: DuckDBStore, date: str) -> int:
    total = 0
    for st in ["0", "1", "2"]:
        data = client.get("/dingpan/radar", {"st": st})
        if not data:
            continue
        events = data.get("data", data.get("events", []))
        if isinstance(events, list):
            for ev in events:
                if isinstance(ev, dict):
                    row = (
                        date,
                        str(ev.get("time", ev.get("Time", ""))),
                        str(ev.get("event_type", ev.get("type", st))),
                        str(ev.get("stock_code", ev.get("Code", ""))),
                        ev.get("stock_name", ev.get("Name", "")),
                        str(ev.get("desc", ev.get("Desc", ""))),
                    )
                    n = store.insert_rows("dingpan_radar", [row],
                        ["date", "time", "event_type", "stock_code", "stock_name", "event_desc"])
                    total += n
    if total:
        store.log_collect("dingpan_radar", "/dingpan/radar", total, "ok")
    return total


def collect_dingpan_weipan(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/dingpan/weipan")
    if not data:
        return 0
    stocks = data.get("stocks", data.get("data", []))
    day = data.get("day", date)
    rows = []
    for s in stocks:
        if isinstance(s, dict):
            rows.append((
                day,
                str(s.get("stock_code", s.get("code", ""))),
                s.get("stock_name", s.get("name", "")),
                s.get("amount", 0),
                s.get("change_pct", 0),
            ))
        elif isinstance(s, (list, tuple)) and len(s) >= 3:
            rows.append((day, str(s[0]), str(s[1]), s[2] if len(s) > 2 else 0,
                         s[3] if len(s) > 3 else 0))
    if rows:
        n = store.insert_rows("dingpan_weipan", rows,
            ["date", "stock_code", "stock_name", "amount", "change_pct"])
        store.log_collect("dingpan_weipan", "/dingpan/weipan", n, "ok")
        return n
    store.insert_raw("/dingpan/weipan", data)
    return 0


def collect_dingpan_jijin(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/dingpan/jijin")
    if not data:
        return 0
    nums = data.get("nums", data.get("data", []))
    day = data.get("day", date)
    rows = []
    for s in nums:
        if isinstance(s, dict):
            rows.append((
                day,
                str(s.get("stock_code", s.get("code", ""))),
                s.get("stock_name", s.get("name", "")),
                s.get("fund_name", s.get("fund", "")),
                s.get("shares", s.get("amount", 0)),
            ))
        elif isinstance(s, (list, tuple)) and len(s) >= 3:
            rows.append((day, str(s[0]), str(s[1]), str(s[2]) if len(s) > 2 else "",
                         s[3] if len(s) > 3 else 0))
    if rows:
        n = store.insert_rows("dingpan_jijin", rows,
            ["date", "stock_code", "stock_name", "fund_name", "shares"])
        store.log_collect("dingpan_jijin", "/dingpan/jijin", n, "ok")
        return n
    store.insert_raw("/dingpan/jijin", data)
    return 0


def collect_dingpan_all(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/dingpan/all")
    if not data:
        return 0
    total = 0
    for cat, val in data.items():
        row = (date, cat, json.dumps(val, ensure_ascii=False))
        n = store.insert_rows("dingpan_all", [row], ["date", "category", "raw_json"])
        total += n
    store.log_collect("dingpan_all", "/dingpan/all", total, "ok")
    return total


def collect_dingpan_art_title(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/dingpan/art-title")
    if not data:
        return 0
    items = data if isinstance(data, list) else data.get("data", [])
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                str(item.get("code", "")),
                item.get("title", ""),
                item.get("url", ""),
            ))
    if rows:
        n = store.insert_rows("dingpan_art_title", rows,
            ["date", "stock_code", "article_title", "article_url"])
        store.log_collect("dingpan_art_title", "/dingpan/art-title", n, "ok")
        return n
    return 0


def collect_all_dingpan(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    return {
        "dingpan_module_versatile": collect_dingpan_module_versatile(client, store, date),
        "dingpan_northbound_close": collect_dingpan_northbound_close(client, store, date),
        "dingpan_southbound_close": collect_dingpan_southbound_close(client, store, date),
        "dingpan_radar": collect_dingpan_radar(client, store, date),
        "dingpan_weipan": collect_dingpan_weipan(client, store, date),
        "dingpan_jijin": collect_dingpan_jijin(client, store, date),
        "dingpan_all": collect_dingpan_all(client, store, date),
        "dingpan_art_title": collect_dingpan_art_title(client, store, date),
    }
