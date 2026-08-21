"""Collectors for ETF, Xianhuo, Theme, Auction, Kline (8 endpoints)."""
import json
from base import KPLClient, DuckDBStore


def _normalize_kline_date(value) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _parse_bidding_anomalies(data, fallback_date: str) -> list[tuple[str, str, str, float]]:
    if not data:
        return []
    if isinstance(data, dict):
        api_date = str(data.get("date") or fallback_date)
        items = data.get("anomalies")
        if items is None:
            items = data.get("data", [])
    elif isinstance(data, list):
        api_date = fallback_date
        items = data
    else:
        return []

    rows = []
    for item in items or []:
        if isinstance(item, dict):
            code = str(item.get("stock_code") or item.get("code") or item.get("stock_id") or "")
            anomaly_type = item.get("anomaly_type") or item.get("type") or "bidding_amount"
            value = (
                item.get("value")
                if item.get("value") is not None
                else item.get("anomaly_value", item.get("amount", item.get("bidding_amount", 0)))
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            code = str(item[0])
            anomaly_type = "bidding_amount"
            value = item[4]
        else:
            continue
        if not code:
            continue
        try:
            anomaly_value = float(value or 0)
        except (TypeError, ValueError):
            anomaly_value = 0.0
        rows.append((api_date, code, str(anomaly_type), anomaly_value))
    return rows


# ============ ETF (2) ============
def collect_etf_ranking(client: KPLClient, store: DuckDBStore, date: str) -> int:
    """API returns {date, etfs: [["name", "pct", code], ...]}"""
    data = client.get("/etf/ranking", {"date": date})
    if not data:
        return 0
    etfs = data.get("etfs", data.get("data", []))
    rows = []
    for e in etfs:
        if isinstance(e, dict):
            rows.append((
                data.get("date", date),
                str(e.get("code", e.get("etf_code", ""))),
                e.get("name", e.get("etf_name", "")),
                e.get("change_pct", 0),
            ))
        elif isinstance(e, (list, tuple)):
            # Format: [name, change_pct, code]
            if len(e) >= 3:
                rows.append((data.get("date", date), str(e[2]), str(e[0]), e[1] if len(e) > 1 else 0))
            elif len(e) >= 2:
                rows.append((data.get("date", date), str(e[1]) if len(e) > 1 else "", str(e[0]), 0))
    if rows:
        n = store.insert_rows("etf_ranking", rows,
            ["date", "etf_code", "etf_name", "change_pct"],
            replace_on=["date", "etf_code"])
        store.log_collect("etf_ranking", "/etf/ranking", n, "ok")
        return n
    store.insert_raw("/etf/ranking", data)
    return 0


def collect_etf_all(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/etf/all", {"date": date})
    if not data:
        return 0
    etfs = data if isinstance(data, list) else data.get("data", data.get("etfs", []))
    rows = []
    for e in etfs:
        if isinstance(e, dict):
            rows.append((
                date,
                str(e.get("code", e.get("etf_code", ""))),
                e.get("name", e.get("etf_name", "")),
                e.get("change_pct", e.get("涨跌幅", 0)),
                e.get("turnover", e.get("成交额", 0)),
            ))
    if rows:
        n = store.insert_rows("etf_all", rows,
            ["date", "etf_code", "etf_name", "change_pct", "turnover"])
        store.log_collect("etf_all", "/etf/all", n, "ok")
        return n
    store.insert_raw("/etf/all", data)
    return 0


# Validated ETF parser used by the daily path.  KPL has returned a
# stock/theme-shaped payload from /etf/all before; only ETF code families are
# accepted and the raw response remains available for audit.
def collect_etf_all_validated(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/etf/all", {"date": date})
    if not data:
        return 0
    etfs = data if isinstance(data, list) else data.get("data", data.get("etfs", []))
    rows = []
    for item in etfs or []:
        if isinstance(item, dict):
            code = str(item.get("code") or item.get("etf_code") or "")
            name = item.get("name") or item.get("etf_name") or ""
            pct, turnover = item.get("change_pct", 0), item.get("turnover", 0)
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            code, name, pct = str(item[0]), str(item[1]), item[2]
            turnover = item[3] if len(item) > 3 else 0
        else:
            continue
        if len(code) == 6 and code.startswith(("15", "16", "51", "56", "58")):
            rows.append((date, code, name, pct, turnover))
    if not rows:
        store.insert_raw("/etf/all", data)
        return 0
    n = store.insert_rows(
        "etf_all", rows,
        ["date", "etf_code", "etf_name", "change_pct", "turnover"],
        replace_on=["date", "etf_code"],
    )
    store.log_collect("etf_all", "/etf/all", n, "ok")
    return n


# ============ Xianhuo (1) ============
def collect_xianhuo(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/xianhuo/list")
    if not data:
        return 0
    items = data.get("list", data.get("data", []))
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                item.get("name", item.get("product_name", "")),
                item.get("price", 0),
                item.get("change_pct", item.get("涨跌幅", 0)),
                json.dumps(item, ensure_ascii=False),
            ))
    if rows:
        n = store.insert_rows("xianhuo_list", rows,
            ["date", "product_name", "price", "change_pct", "raw_json"])
        store.log_collect("xianhuo_list", "/xianhuo/list", n, "ok")
        return n
    store.insert_raw("/xianhuo/list", data)
    return 0


# ============ Theme (1) ============
def collect_theme_hot(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/theme/hot")
    if not data:
        return 0
    themes = data.get("themes", data.get("data", []))
    rows = []
    for t in themes:
        if isinstance(t, dict):
            rows.append((
                date,
                t.get("Name", t.get("name", "")),
                str(t.get("ID", t.get("code", ""))),
                t.get("change_pct", t.get("涨跌幅", 0)),
                t.get("leader_stock", t.get("领涨股", "")),
            ))
    if rows:
        n = store.insert_rows("theme_hot", rows,
            ["date", "theme_name", "theme_code", "change_pct", "leader_stock"],
            replace_on=["date", "theme_code"])
        store.log_collect("theme_hot", "/theme/hot", n, "ok")
        return n
    store.insert_raw("/theme/hot", data)
    return 0


# ============ Auction (2) ============
def collect_auction_tick(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/auction/tick", {"code": code, "date": date})
        if not data:
            continue
        # The endpoint nests ticks under "auction_ticks"; earlier parser
        # revisions looked at "data"/"ticks" and silently stored nothing
        # (auction_tick frozen at 2026-07-14 despite a healthy endpoint).
        if isinstance(data, dict):
            ticks = data.get("auction_ticks") or data.get("data") or data.get("ticks") or []
        elif isinstance(data, list):
            ticks = data
        else:
            ticks = []
        rows = []
        for tk in ticks:
            if isinstance(tk, dict):
                rows.append((
                    date, code,
                    str(tk.get("time", tk.get("t", ""))),
                    tk.get("price", tk.get("p", 0)),
                    tk.get("volume", tk.get("v", 0)),
                ))
            elif isinstance(tk, (list, tuple)) and len(tk) >= 3:
                rows.append((date, code, str(tk[0]), tk[1], tk[2]))
        if rows:
            n = store.insert_rows("auction_tick", rows,
                ["date", "stock_code", "time", "price", "volume"],
                replace_on=["date", "stock_code", "time"])
            total += n
    if total:
        store.log_collect("auction_tick", "/auction/tick", total, "ok")
    return total


def collect_auction_bidding_anomaly(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    if not stock_codes:
        return 0
    total = 0
    last_data = None
    # The endpoint must be queried per stock; iterate all codes instead of
    # only the first (the original single-code bug silently dropped the rest).
    for code in stock_codes:
        data = client.get("/auction/bidding-anomaly", {"code": code, "date": date})
        if data:
            last_data = data
        rows = _parse_bidding_anomalies(data, date)
        if not rows:
            continue
        total += store.insert_rows(
            "auction_bidding_anomaly",
            rows,
            ["date", "stock_code", "anomaly_type", "anomaly_value"],
            replace_on=["date", "stock_code", "anomaly_type"],
        )
    if total:
        store.log_collect("auction_bidding_anomaly", "/auction/bidding-anomaly", total, "ok")
    elif last_data:
        store.insert_raw("/auction/bidding-anomaly", last_data)
    return total


# ============ Kline (1) ============
def collect_kline(client: KPLClient, store: DuckDBStore, date: str, stock_code: str) -> int:
    data = client.get("/kline", {"code": stock_code, "ktype": "d", "count": "5"})
    if not data:
        return 0
    klines = data if isinstance(data, list) else data.get("data", data.get("klines", []))
    rows = []
    for kl in klines:
        if isinstance(kl, dict):
            rows.append((
                _normalize_kline_date(kl.get("date", date)), stock_code,
                kl.get("open", kl.get("o", 0)),
                kl.get("high", kl.get("h", 0)),
                kl.get("low", kl.get("l", 0)),
                kl.get("close", kl.get("c", 0)),
                kl.get("volume", kl.get("v", 0)),
                kl.get("turnover", kl.get("amount", 0)),
                kl.get("change_pct", kl.get("pct", 0)),
                "d",
            ))
        elif isinstance(kl, (list, tuple)) and len(kl) >= 6:
            rows.append((
                _normalize_kline_date(kl[0]), stock_code, kl[1], kl[2], kl[3], kl[4],
                kl[5] if len(kl) > 5 else 0,
                kl[6] if len(kl) > 6 else 0,
                kl[7] if len(kl) > 7 else 0,
                "d",
            ))
    if rows:
        n = store.insert_rows("kline", rows,
            ["date", "stock_code", "open", "high", "low", "close",
             "volume", "turnover", "change_pct", "ktype"])
        store.log_collect("kline", "/kline", n, "ok")
        return n
    return 0


def collect_all_misc(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list = None) -> dict:
    results = {}
    results["etf_ranking"] = collect_etf_ranking(client, store, date)
    results["etf_all"] = collect_etf_all_validated(client, store, date)
    results["xianhuo"] = collect_xianhuo(client, store, date)
    results["theme_hot"] = collect_theme_hot(client, store, date)
    if stock_codes:
        # Auction only for active stocks (limit to top 50 to avoid too many requests)
        active = stock_codes[:50]
        results["auction_tick"] = collect_auction_tick(client, store, date, active)
        results["auction_bidding_anomaly"] = collect_auction_bidding_anomaly(client, store, date, active)
    else:
        results["auction_tick"] = 0
        results["auction_bidding_anomaly"] = 0
    return results
