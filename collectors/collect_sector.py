"""Sector data collectors (19 endpoints)."""
import json
from datetime import datetime
from contextlib import contextmanager
from trade_system.sector_contract import envelope, members, number, strength
from trade_system.data_store import KPLClient, DuckDBStore, logger


@contextmanager
def _publication(store):
    """Nested collector scopes share only this explicit sector publication."""
    if getattr(store, '_sector_publication', False):
        yield
    else:
        with store.transaction():
            store._sector_publication = True
            try:
                yield
            finally:
                store._sector_publication = False


def _ranking_rows(data, date):
    envelope(data, date)
    if not isinstance(data, dict) or not isinstance(data.get('sectors'), list):
        raise ValueError('sector ranking: sectors list required')
    sectors = data['sectors']
    identities = _validated_plate_rows({'plates': [
        {'code': s.get('sector_code', s.get('code')), 'name': s.get('sector_name', s.get('name', ''))}
        if isinstance(s, dict) else s for s in sectors]})
    rows, stock_rows = [], []
    for item, (code, name, _) in zip(sectors, identities):
        if not isinstance(item, dict):
            raise ValueError('sector ranking: object row required')
        envelope(item, date, code)
        rows.append((date, code, name, number(item.get('stock_count'), count=True)))
        parsed = members({'stocks': item.get('stocks', [])}, date, code)
        # Also validate explicit dates/identities on the nested member objects.
        stock_rows.extend(row[:4] for row in parsed)
    return rows, stock_rows


def _validated_plate_rows(data):
    """Parse once for both catalogue and ranking fallback, before any writes."""
    if data is None:
        return []
    if not isinstance(data, dict) or not any(key in data for key in ("plates", "data")):
        raise ValueError("sector plates: catalogue response required; original catalogue retained")
    plates = data.get("plates", data.get("data"))
    if not isinstance(plates, list):
        raise ValueError("sector plates: list required; original catalogue retained")
    rows = []
    seen = set()
    for p in plates:
        if isinstance(p, dict):
            code = p.get("code", p.get("plate_code"))
            name = p.get("name", p.get("plate_name", ""))
            ptype = p.get("type", p.get("sector_type", ""))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            code, name = p[:2]
            ptype = p[2] if len(p) > 2 else ""
        else:
            raise ValueError("sector plates: invalid row; original catalogue retained")
        if (isinstance(code, bool) or not isinstance(code, (str, int))
                or not str(code).strip() or not isinstance(name, str)
                or isinstance(ptype, bool) or not isinstance(ptype, (str, int))):
            raise ValueError("sector plates: invalid identity or metadata; original catalogue retained")
        code = str(code).strip()
        if code in seen:
            raise ValueError("sector plates: duplicate identity; original catalogue retained")
        seen.add(code)
        rows.append((code, name, str(ptype)))
    return rows


def collect_sector_plates(client: KPLClient, store: DuckDBStore, date: str) -> list:
    """Merge a validated catalogue; absence is not evidence of sector retirement.

    This endpoint provides no qualified full-snapshot receipt. Even a nonempty
    response can be partial, so it cannot authorize deletion of old identifiers.
    Downstream collectors receive the retained catalogue, not just this batch.
    """
    rows = _validated_plate_rows(client.get("/sector/plates"))
    if rows:
        received = datetime.now()
        with _publication(store):
            store.insert_rows("sector_plates", [(*row, received) for row in rows],
                ["sector_code", "sector_name", "sector_type", "fetched_at"],
                replace_on=["sector_code"])
        store.log_collect("sector_plates", "/sector/plates", len(rows), "merged_unverified_completeness")
    return store.fetchall("SELECT sector_code,sector_name FROM sector_plates ORDER BY sector_code")


def collect_sector_ranking(client: KPLClient, store: DuckDBStore, date: str) -> int:
    """Fetch sector ranking. API returns {summary, sectors: [{sector_code, sector_name, stock_count, stocks}]}.

    This is the single most critical endpoint — it provides both sector metadata AND stock codes.
    Uses critical=True to enable aggressive backoff on empty responses.
    """
    data = client.get("/sector/ranking", {"date": date}, critical=True)
    if not data:
        return 0
    rows, all_stock_rows = _ranking_rows(data, date)
    if not data['sectors']:
        return _collect_sector_ranking_fallback(client, store, date)

    total = 0
    received = datetime.now()
    # No qualified completeness receipt: absent sectors/stocks are retained.
    # Ranking and its inline stock projection are one publication transaction.
    with _publication(store):
        if rows:
            n = store.insert_rows("sector_ranking", [(*row, received) for row in rows],
                ["date", "sector_code", "sector_name", "stock_count", "fetched_at"], replace_on=['date','sector_code'])
            store.log_collect("sector_ranking", "/sector/ranking", n, "merged_unverified_completeness")
            total += n

        if all_stock_rows:
            # Inline membership does not refresh old quote fields. Clear only
            # those fields on returned keys, while absent keys retain old clocks.
            n2 = store.insert_rows("sector_stocks", [(*row, None, None, None, None, received) for row in all_stock_rows],
                ["date", "sector_code", "stock_code", "stock_name", "change_pct", "turnover", "market_cap", "raw_json", "fetched_at"],
                replace_on=['date','sector_code','stock_code'])
            store.log_collect("sector_stocks", "/sector/ranking(stocks)", n2, "merged_unverified_completeness")
            total += n2

    # Also log the summary if present
    summary = data.get("summary", {})
    if summary:
        logger.info(f"  Sector ranking summary: {json.dumps(summary, ensure_ascii=False)[:120]}")

    return total


def collect_sector_strength(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    rows = []
    received = datetime.now()
    for code in sector_codes:
        data = client.get('/sector/strength', {'code': code, 'date': date})
        parsed = strength(data, date, code)
        if parsed is not None:
            rows.append((*parsed, json.dumps(data, ensure_ascii=False), received))
    with _publication(store):
        total = store.insert_rows('sector_strength', rows,
            ['date','sector_code','strength_value','zhangting','fengban_rate',
             'dieting','up_count','down_count','raw_json','fetched_at'], replace_on=['date','sector_code']) if rows else 0
        if total:
            store.log_collect('sector_strength', '/sector/strength', total, 'merged_unverified_completeness')
    return total


def collect_sector_stocks(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    rows = []
    received = datetime.now()
    for code in sector_codes:
        data = client.get('/sector/stocks', {'code': code, 'date': date})
        rows.extend((*row, None, received) for row in members(data, date, code))
    # Validate the whole multi-sector response before changing any member key.
    with _publication(store):
        total = store.insert_rows('sector_stocks', rows,
            ['date','sector_code','stock_code','stock_name','change_pct','turnover',
             'market_cap','raw_json','fetched_at'], replace_on=['date','sector_code','stock_code']) if rows else 0
        if total:
            store.log_collect('sector_stocks', '/sector/stocks', total, 'merged_unverified_completeness')
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
                ["date", "sector_code", "time", "price", "volume"],
                replace_on=["date", "sector_code", "time"])
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

    plate_rows = _validated_plate_rows(plates_data)
    codes = dict(store.fetchall('SELECT sector_code,sector_name FROM sector_plates ORDER BY sector_code'))
    codes.update((code, name) for code, name, _ in plate_rows)

    ranking_rows = []
    stock_rows = []
    strength_rows = []

    received = datetime.now()
    for code, name in codes.items():
        strength_data = client.get('/sector/strength', {'code': code, 'date': date})
        parsed = strength(strength_data, date, code)
        stocks_data = client.get('/sector/stocks', {'code': code, 'date': date})
        parsed_members = members(stocks_data, date, code)
        if parsed is not None:
            # Neither limit-up counts nor a possibly partial member list prove
            # the provider's stock_count. Keep it unknown unless explicitly supplied.
            ranking_rows.append((date, code, name or f'Sector_{code}', number(strength_data.get('stock_count'), count=True)))
            strength_rows.append((*parsed, json.dumps(strength_data, ensure_ascii=False), received))
        stock_rows.extend(row[:4] for row in parsed_members)

    total = 0
    received = datetime.now()
    with _publication(store):
        if ranking_rows:
            n = store.insert_rows("sector_ranking", [(*row, received) for row in ranking_rows],
                ["date", "sector_code", "sector_name", "stock_count", "fetched_at"], replace_on=['date','sector_code'])
            store.log_collect("sector_ranking", "/sector/ranking(fallback)", n, "merged_unverified_completeness")
            total += n
            logger.info(f"  Fallback: {n} sector ranking rows from per-sector strength")

        if strength_rows:
            # Don't delete existing — may have come from other sources
            n2 = store.insert_rows("sector_strength", strength_rows,
                ["date", "sector_code", "strength_value", "zhangting", "fengban_rate",
                 "dieting", "up_count", "down_count", "raw_json", "fetched_at"], replace_on=['date','sector_code'])
            store.log_collect("sector_strength", "/sector/strength(fallback)", n2, "ok")
            total += n2

        if stock_rows:
            n3 = store.insert_rows("sector_stocks", [(*row, None, None, None, None, received) for row in stock_rows],
                ["date", "sector_code", "stock_code", "stock_name", "change_pct", "turnover", "market_cap", "raw_json", "fetched_at"],
                replace_on=['date','sector_code','stock_code'])
            store.log_collect("sector_stocks", "/sector/stocks(fallback)", n3, "merged_unverified_completeness")
            total += n3
            logger.info(f"  Fallback: {n3} stock-code rows from per-sector stocks")

    return total


def collect_all_sector(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    results = {}
    class PreparedClient:
        def __init__(self):
            self.cache = {}
        def get(self, endpoint, params=None, **kwargs):
            key = (endpoint, tuple(sorted((params or {}).items())))
            if key not in self.cache:
                self.cache[key] = client.get(endpoint, params, **kwargs) if params is not None else client.get(endpoint, **kwargs)
            return self.cache[key]
    prepared = PreparedClient()
    plates_data = prepared.get('/sector/plates')
    incoming = _validated_plate_rows(plates_data)
    codes = dict(store.fetchall('SELECT sector_code,sector_name FROM sector_plates ORDER BY sector_code'))
    codes.update((code, name) for code, name, _ in incoming)
    ranking = prepared.get('/sector/ranking', {'date': date}, critical=True)
    if ranking is not None:
        ranked, _ = _ranking_rows(ranking, date)
        codes.update((row[1],row[2]) for row in ranked)
    if not codes:
        codes.update(store.fetchall('SELECT DISTINCT sector_code,sector_name FROM sector_ranking WHERE date=?', [date]))
    sector_codes = sorted(codes)
    for code in sector_codes:
        strength(prepared.get('/sector/strength', {'code':code,'date':date}), date, code)
        members(prepared.get('/sector/stocks', {'code':code,'date':date}), date, code)
    # These reads are now cached; no network is held inside this transaction.
    with _publication(store):
        plates = collect_sector_plates(prepared, store, date)
        results['sector_plates'] = len(plates)
        results['sector_ranking'] = collect_sector_ranking(prepared, store, date)
        results['sector_stocks_full'] = collect_sector_stocks(prepared, store, date, sector_codes)
        results['sector_strength'] = collect_sector_strength(prepared, store, date, sector_codes)
    results['sector_all_stocks'] = collect_sector_all_stocks(client, store, date, sector_codes)

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
