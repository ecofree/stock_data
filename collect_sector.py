"""Sector data collectors (19 endpoints)."""
import json
import time
from datetime import datetime
from base import KPLClient, DuckDBStore, logger


def collect_sector_plates(client: KPLClient, store: DuckDBStore, date: str) -> list:
    """Fetch all sector plates and return list of (code, name) for downstream use."""
    data = client.get("/sector/plates")
    if not data:
        return []
    plates = data.get("plates", data.get("data", []))
    rows = []
    result = []
    for p in plates:
        if isinstance(p, dict):
            code = str(p.get("code", p.get("plate_code", "")))
            name = p.get("name", p.get("plate_name", ""))
            ptype = p.get("type", p.get("sector_type", ""))
            if code:
                rows.append((code, name, str(ptype)))
                result.append((code, name))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            code = str(p[0])
            name = str(p[1])
            ptype = str(p[2]) if len(p) > 2 else ""
            rows.append((code, name, ptype))
            result.append((code, name))
    if rows:
        # Use REPLACE for upsert on sector_plates
        store.execute("DELETE FROM sector_plates")
        store.insert_rows("sector_plates", rows, ["sector_code", "sector_name", "sector_type"])
        store.log_collect("sector_plates", "/sector/plates", len(rows), "ok")
    return result


def collect_sector_ranking(client: KPLClient, store: DuckDBStore, date: str) -> int:
    """Fetch sector ranking. API returns {summary, sectors: [{sector_code, sector_name, stock_count, stocks}]}.

    This is the single most critical endpoint — it provides both sector metadata AND stock codes.
    Uses critical=True to enable aggressive backoff on empty responses.
    """
    data = client.get("/sector/ranking", {"date": date}, critical=True)
    if not data:
        return 0
    sectors = data.get("sectors", [])

    # Rate-limit fallback: if ranking is empty, try building from individual sector/strength calls
    if not sectors:
        logger.warning("  sector/ranking returned empty — attempting fallback via per-sector strength...")
        return _collect_sector_ranking_fallback(client, store, date)
    rows = []
    all_stock_rows = []
    for s in sectors:
        if not isinstance(s, dict):
            continue
        scode = str(s.get("sector_code", s.get("code", "")))
        sname = s.get("sector_name", s.get("name", ""))
        scount = s.get("stock_count", 0)
        if not scode:
            continue

        # Store sector-level metadata
        rows.append((date, scode, sname, scount))

        # Also extract constituent stocks if available
        stocks = s.get("stocks", [])
        if isinstance(stocks, list):
            for st in stocks:
                if isinstance(st, dict):
                    all_stock_rows.append((
                        date, scode,
                        str(st.get("股票代码", st.get("stock_code", ""))),
                        st.get("股票名称", st.get("stock_name", "")),
                    ))

    total = 0
    if rows:
        # Use DELETE+INSERT to replace old data for this date
        store.execute("DELETE FROM sector_ranking WHERE date = ?", [date])
        n = store.insert_rows("sector_ranking", rows,
            ["date", "sector_code", "sector_name", "stock_count"])
        store.log_collect("sector_ranking", "/sector/ranking", n, "ok")
        total += n

    if all_stock_rows:
        store.execute("DELETE FROM sector_stocks WHERE date = ?", [date])
        n2 = store.insert_rows("sector_stocks", all_stock_rows,
            ["date", "sector_code", "stock_code", "stock_name"])
        store.log_collect("sector_stocks", "/sector/ranking(stocks)", n2, "ok")
        total += n2

    # Also log the summary if present
    summary = data.get("summary", {})
    if summary:
        logger.info(f"  Sector ranking summary: {json.dumps(summary, ensure_ascii=False)[:120]}")

    return total


def collect_sector_strength(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    """API returns {sector_code, date, strength, zhangting, fengban, dieting, up_count, down_count}."""
    total = 0
    for code in sector_codes:
        data = client.get("/sector/strength", {"code": code, "date": date})
        if not data:
            continue
        if isinstance(data, dict):
            row = (
                data.get("date", date), code,
                data.get("strength", data.get("强度", 0)),
                data.get("zhangting", data.get("涨停", 0)),
                data.get("fengban", data.get("封板率", 0)),
                data.get("dieting", data.get("跌停", 0)),
                data.get("up_count", data.get("上涨家数", 0)),
                data.get("down_count", data.get("下跌家数", 0)),
            )
            n = store.insert_rows("sector_strength", [row],
                ["date", "sector_code", "strength_value", "zhangting", "fengban_rate",
                 "dieting", "up_count", "down_count"])
            total += n
    if total:
        store.log_collect("sector_strength", "/sector/strength", total, "ok")
    return total


def collect_sector_stocks(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes:
        data = client.get("/sector/stocks", {"code": code, "date": date})
        if not data:
            continue
        stocks = data.get("stocks", data.get("data", []))
        if isinstance(data, list):
            stocks = data
        rows = []
        for s in stocks:
            if isinstance(s, dict):
                rows.append((
                    date, code,
                    str(s.get("stock_code", s.get("code", s.get("股票代码", "")))),
                    s.get("stock_name", s.get("name", s.get("股票名称", ""))),
                    s.get("change_pct", s.get("涨跌幅", 0)),
                    s.get("turnover", s.get("成交额", 0)),
                    s.get("market_cap", s.get("流通市值", 0)),
                ))
            elif isinstance(s, (list, tuple)) and len(s) >= 3:
                rows.append((
                    date, code,
                    str(s[0]), str(s[1]),
                    s[2] if len(s) > 2 else 0,
                    s[3] if len(s) > 3 else 0,
                    s[4] if len(s) > 4 else 0,
                ))
        if rows:
            n = store.insert_rows("sector_stocks", rows,
                ["date", "sector_code", "stock_code", "stock_name",
                 "change_pct", "turnover", "market_cap"])
            total += n
    if total:
        store.log_collect("sector_stocks", "/sector/stocks", total, "ok")
    return total


def collect_sector_capital(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    """Collect directional sector-flow rows without promoting quote placeholders.

    ``/sector/capital`` is a per-sector compatibility endpoint and, when it is
    blocked or still pre-open, it may return a syntactically valid dictionary
    with all flow fields empty.  Those rows must not make the legacy
    ``sector_capital`` table look current.  The full-market sector collector
    uses the source-aware batch table; this function remains the bounded KPL
    fast tier.
    """

    def value(data: dict, *keys):
        for key in keys:
            candidate = data.get(key)
            if candidate not in (None, "", "-"):
                return candidate
        return None

    total = 0
    for code in sector_codes:
        data = client.get("/sector/capital", {"code": code, "date": date})
        if not isinstance(data, dict) or not data:
            continue
        response_date = str(data.get("date") or data.get("trade_date") or date)
        if response_date[:10] != str(date)[:10]:
            # Never relabel a historical response as today's intraday flow.
            continue
        main_net = value(data, "main_net_inflow", "main_net")
        super_net = value(data, "super_net_inflow", "super_net", "main_sell")
        big_net = value(data, "big_net_inflow", "big_net", "net_amount")
        mid_net = value(data, "mid_net_inflow", "mid_net")
        small_net = value(data, "small_net_inflow", "small_net")
        if all(item is None for item in (main_net, super_net, big_net, mid_net, small_net)):
            # Quote/name-only or blocked responses are not capital-flow data.
            continue
        row = (response_date[:10], code, main_net, super_net, big_net, mid_net, small_net)
        n = store.insert_rows("sector_capital", [row],
            ["date", "sector_code", "main_net_inflow", "super_net_inflow",
             "big_net_inflow", "mid_net_inflow", "small_net_inflow"])
        total += n
    if total:
        store.log_collect("sector_capital", "/sector/capital", total, "ok")
    return total


def collect_sector_boom_reason(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes:
        data = client.get("/sector/boom-reason", {"code": code, "date": date})
        if not data:
            continue
        if isinstance(data, dict):
            reason = data.get("reason", data.get("涨停原因", ""))
            row = (data.get("date", date), code, str(reason))
            n = store.insert_rows("sector_boom_reason", [row],
                ["date", "sector_code", "reason"])
            total += n
    if total:
        store.log_collect("sector_boom_reason", "/sector/boom-reason", total, "ok")
    return total


def collect_sector_all_stocks(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:50]:
        data = client.get("/sector/all-stocks", {"code": code})
        if not data:
            continue
        stocks = data if isinstance(data, list) else data.get("stocks", data.get("data", []))
        rows = []
        for stock in stocks:
            if isinstance(stock, dict):
                stock_code = str(stock.get("stock_code", stock.get("code", stock.get("鑲＄エ浠ｇ爜", ""))))
                if not stock_code:
                    continue
                rows.append(
                    (
                        date,
                        code,
                        stock_code,
                        stock.get("stock_name", stock.get("name", stock.get("鑲＄エ鍚嶇О", ""))),
                    )
                )
            elif isinstance(stock, (list, tuple)) and len(stock) >= 2:
                rows.append((date, code, str(stock[0]), str(stock[1])))
        if rows:
            total += store.insert_rows(
                "sector_all_stocks",
                rows,
                ["date", "sector_code", "stock_code", "stock_name"],
                replace_on=["date", "sector_code", "stock_code"],
            )
    if total:
        store.log_collect("sector_all_stocks", "/sector/all-stocks", total, "ok")
    return total


def collect_sector_son_plates(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:50]:  # Limit to avoid too many requests
        data = client.get("/sector/son-plates", {"code": code})
        if not data:
            continue
        subs = data if isinstance(data, list) else data.get("data", data.get("plates", []))
        rows = []
        for s in subs:
            if isinstance(s, dict):
                rows.append((
                    code,
                    str(s.get("code", s.get("son_code", ""))),
                    s.get("name", s.get("son_name", "")),
                ))
        if rows:
            n = store.insert_rows(
                "sector_son_plates",
                rows,
                ["parent_code", "son_code", "son_name"],
                replace_on=["parent_code", "son_code"],
            )
            total += n
    if total:
        store.log_collect("sector_son_plates", "/sector/son-plates", total, "ok")
    return total


def collect_sector_sub_plates(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    return collect_sector_son_plates(client, store, date, sector_codes)


def collect_sector_sub_concepts(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:50]:
        data = client.get("/sector/sub-concepts", {"code": code})
        if not data:
            continue
        concepts = data if isinstance(data, list) else data.get("concepts", data.get("data", data.get("plates", [])))
        rows = []
        for concept in concepts:
            if isinstance(concept, dict):
                concept_code = str(concept.get("code", concept.get("concept_code", concept.get("id", ""))))
                if not concept_code:
                    continue
                rows.append(
                    (
                        code,
                        concept_code,
                        concept.get("name", concept.get("concept_name", "")),
                    )
                )
            elif isinstance(concept, (list, tuple)) and len(concept) >= 2:
                rows.append((code, str(concept[0]), str(concept[1])))
        if rows:
            total += store.insert_rows(
                "sector_sub_concepts",
                rows,
                ["sector_code", "concept_code", "concept_name"],
                replace_on=["sector_code", "concept_code"],
            )
    if total:
        store.log_collect("sector_sub_concepts", "/sector/sub-concepts", total, "ok")
    return total


def collect_sector_parent(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:50]:
        data = client.get("/sector/parent-plate", {"code": code})
        if not data:
            continue
        if isinstance(data, dict):
            row = (
                code,
                str(data.get("parent_code", data.get("code", ""))),
                data.get("parent_name", data.get("name", "")),
            )
            n = store.insert_rows("sector_parent_plate", [row],
                ["sector_code", "parent_code", "parent_name"])
            total += n
    if total:
        store.log_collect("sector_parent_plate", "/sector/parent-plate", total, "ok")
    return total


def collect_sector_plate_info_qj(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:20]:
        data = client.get("/sector/plate-info-qj", {"code": code})
        if not data:
            continue
        row = (date, code, json.dumps(data, ensure_ascii=False))
        n = store.insert_rows("sector_plate_info_qj", [row],
            ["date", "sector_code", "raw_json"])
        total += n
    if total:
        store.log_collect("sector_plate_info_qj", "/sector/plate-info-qj", total, "ok")
    return total


def collect_sector_bk_fenshi_zhibo(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:20]:
        data = client.get("/sector/bk-fenshi-zhibo", {"code": code})
        if not data:
            continue
        pts = data if isinstance(data, list) else data.get("data", data.get("points", []))
        rows = []
        for pt in pts:
            if isinstance(pt, dict):
                rows.append((
                    date, code,
                    str(pt.get("time", "")),
                    pt.get("price", 0),
                    pt.get("volume", 0),
                ))
        if rows:
            n = store.insert_rows("sector_bk_fenshi_zhibo", rows,
                ["date", "sector_code", "time", "price", "volume"])
            total += n
    if total:
        store.log_collect("sector_bk_fenshi_zhibo", "/sector/bk-fenshi-zhibo", total, "ok")
    return total


def collect_sector_strength_batch(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    codes_str = ",".join(sector_codes[:30])
    data = client.get("/sector/strength-batch", {"codes": codes_str, "date": date})
    if not data:
        return 0
    items = data if isinstance(data, list) else data.get("data", [])
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                str(item.get("code", item.get("sector_code", ""))),
                item.get("strength", item.get("强度", 0)),
            ))
    if rows:
        n = store.insert_rows("sector_strength_batch", rows,
            ["date", "sector_code", "strength_value"],
            replace_on=["date", "sector_code"])
        store.log_collect("sector_strength_batch", "/sector/strength-batch", n, "ok")
        return n
    store.insert_raw("/sector/strength-batch", data)
    return 0


def collect_sector_strength_ndays(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    """Requires end_date param (not date)."""
    total = 0
    for code in sector_codes[:30]:
        data = client.get("/sector/strength-ndays", {"code": code, "end_date": date, "days": "5"})
        if not data:
            continue
        row = (date, code, json.dumps(data, ensure_ascii=False))
        n = store.insert_rows("sector_strength_ndays", [row],
            ["date", "sector_code", "raw_json"])
        total += n
    if total:
        store.log_collect("sector_strength_ndays", "/sector/strength-ndays", total, "ok")
    return total


def collect_sector_strength_dataframe(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    """Requires start + end params."""
    from datetime import timedelta
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    total = 0
    for code in sector_codes[:20]:
        data = client.get("/sector/strength-dataframe", {"code": code, "start": start, "end": date})
        if not data:
            continue
        row = (date, code, json.dumps(data, ensure_ascii=False))
        n = store.insert_rows("sector_strength_dataframe", [row],
            ["date", "sector_code", "raw_json"])
        total += n
    if total:
        store.log_collect("sector_strength_dataframe", "/sector/strength-dataframe", total, "ok")
    return total


def _collect_sector_ranking_fallback(client, store, date):
    """Fallback: use sector/plates codes + per-sector strength to rebuild ranking data.

    Called when /sector/ranking returns empty (rate-limited).
    Queries /sector/strength for each plate code to get: strength, zhangting, dieting, etc.
    Also queries /sector/stocks to get constituent names.
    Returns total rows inserted.
    """
    # Get plate codes
    plates_data = client.get("/sector/plates")
    if not plates_data:
        logger.warning("  Fallback failed: /sector/plates also returned empty")
        return 0

    plates = plates_data.get("plates", [])
    codes = [(str(p.get("code", "")), p.get("name", "")) for p in plates if isinstance(p, dict)]
    codes = [(c, n) for c, n in codes if c]

    ranking_rows = []
    stock_rows = []
    strength_rows = []

    for code, name in codes:
        # Get strength for ranking data
        strength_data = client.get("/sector/strength", {"code": code, "date": date})
        if strength_data and isinstance(strength_data, dict):
            # Add to ranking (simulate what /sector/ranking would give)
            zhangting = strength_data.get("zhangting", 0)
            if name == "" and strength_data.get("sector_code"):
                name = f"Sector_{code}"

            ranking_rows.append((date, code, name or f"Sector_{code}", zhangting or 0))

            # Store full strength
            strength_rows.append((
                strength_data.get("date", date), code,
                strength_data.get("strength", 0),
                strength_data.get("zhangting", 0),
                strength_data.get("fengban", 0),
                strength_data.get("dieting", 0),
                strength_data.get("up_count", 0),
                strength_data.get("down_count", 0),
            ))

        # Get constituent stocks
        stocks_data = client.get("/sector/stocks", {"code": code, "date": date})
        if stocks_data:
            stock_list = stocks_data if isinstance(stocks_data, list) else stocks_data.get("stocks", stocks_data.get("data", []))
            if isinstance(stock_list, list):
                for s in stock_list:
                    if isinstance(s, dict):
                        stock_rows.append((
                            date, code,
                            str(s.get("stock_code", s.get("code", s.get("股票代码", "")))),
                            s.get("stock_name", s.get("name", s.get("股票名称", ""))),
                        ))
                    elif isinstance(s, (list, tuple)) and len(s) >= 2:
                        stock_rows.append((date, code, str(s[0]), str(s[1])))

    total = 0
    if ranking_rows:
        store.execute("DELETE FROM sector_ranking WHERE date = ?", [date])
        n = store.insert_rows("sector_ranking", ranking_rows,
            ["date", "sector_code", "sector_name", "stock_count"])
        store.log_collect("sector_ranking", "/sector/ranking(fallback)", n, "ok")
        total += n
        logger.info(f"  Fallback: {n} sector ranking rows from per-sector strength")

    if strength_rows:
        # Don't delete existing — may have come from other sources
        n2 = store.insert_rows("sector_strength", strength_rows,
            ["date", "sector_code", "strength_value", "zhangting", "fengban_rate",
             "dieting", "up_count", "down_count"])
        store.log_collect("sector_strength", "/sector/strength(fallback)", n2, "ok")
        total += n2

    if stock_rows:
        store.execute("DELETE FROM sector_stocks WHERE date = ?", [date])
        n3 = store.insert_rows("sector_stocks", stock_rows,
            ["date", "sector_code", "stock_code", "stock_name"])
        store.log_collect("sector_stocks", "/sector/stocks(fallback)", n3, "ok")
        total += n3
        logger.info(f"  Fallback: {n3} stock-code rows from per-sector stocks")

    return total


def collect_all_sector(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    results = {}
    results["sector_plates"] = 0
    # sector_ranking now also populates sector_stocks (either directly or via fallback)
    results["sector_ranking"] = collect_sector_ranking(client, store, date)

    # Get all plate codes from the API directly (sector_plates)
    plates = collect_sector_plates(client, store, date)
    results["sector_plates"] = len(plates)
    sector_codes = [p[0] for p in plates]

    # Fallback: if sector_plates returned empty, extract codes from sector_ranking's saved data
    if not sector_codes:
        try:
            sector_codes = [row[0] for row in store.fetchall(
                "SELECT DISTINCT sector_code FROM sector_ranking WHERE date = ?", [date]
            )]
        except Exception:
            pass

    logger.info(f"  Found {len(sector_codes)} sectors, collecting per-sector data...")

    # Also collect full per-sector stocks (more comprehensive than ranking-inline stocks)
    results["sector_stocks_full"] = collect_sector_stocks(client, store, date, sector_codes)
    results["sector_all_stocks"] = collect_sector_all_stocks(client, store, date, sector_codes)

    results["sector_strength"] = collect_sector_strength(client, store, date, sector_codes)
    results["sector_capital"] = collect_sector_capital(client, store, date, sector_codes)
    results["sector_boom_reason"] = collect_sector_boom_reason(client, store, date, sector_codes)
    results["sector_son_plates"] = collect_sector_son_plates(client, store, date, sector_codes)
    results["sector_sub_concepts"] = collect_sector_sub_concepts(client, store, date, sector_codes)
    results["sector_parent"] = collect_sector_parent(client, store, date, sector_codes)
    results["sector_plate_info_qj"] = collect_sector_plate_info_qj(client, store, date, sector_codes)
    results["sector_bk_fenshi_zhibo"] = collect_sector_bk_fenshi_zhibo(client, store, date, sector_codes)
    results["sector_strength_batch"] = collect_sector_strength_batch(client, store, date, sector_codes)
    results["sector_strength_ndays"] = collect_sector_strength_ndays(client, store, date, sector_codes)
    results["sector_strength_dataframe"] = collect_sector_strength_dataframe(client, store, date, sector_codes)

    return results
