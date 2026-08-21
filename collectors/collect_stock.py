"""Stock deep data collectors (8 endpoints)."""
import json
from base import KPLClient, DuckDBStore, logger


def collect_stock_company_info(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/stock/company-info", {"code": code})
        if not data:
            continue
        row = (
            code,
            data.get("company_name", data.get("公司名称", "")),
            data.get("industry", data.get("行业", "")),
            data.get("market_cap", data.get("市值", 0)),
            data.get("description", data.get("简介", "")),
            json.dumps(data, ensure_ascii=False),
        )
        try:
            store.execute("DELETE FROM stock_company_info WHERE stock_code = ?", [code])
            n = store.insert_rows("stock_company_info", [row],
                ["stock_code", "company_name", "industry", "market_cap", "description", "raw_json"],
                replace_on=["stock_code"])
            total += n
        except Exception as e:
            logger.debug(f"stock_company_info error for {code}: {e}")
    if total:
        store.log_collect("stock_company_info", "/stock/company-info", total, "ok")
    return total


def collect_stock_tags(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/stock/gpcphbts-tag", {"code": code})
        if not data:
            continue
        tags = data if isinstance(data, list) else data.get("data", data.get("tags", []))
        rows = []
        for tag in tags:
            if isinstance(tag, dict):
                rows.append((
                    code,
                    tag.get("name", tag.get("标签名称", "")),
                    tag.get("type", tag.get("标签类型", "")),
                ))
            elif isinstance(tag, str):
                rows.append((code, tag, ""))
        if rows:
            n = store.insert_rows("stock_tags", rows,
                ["stock_code", "tag_name", "tag_type"])
            total += n
    if total:
        store.log_collect("stock_tags", "/stock/gpcphbts-tag", total, "ok")
    return total


def collect_stock_institutional_positions(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/stock/institutional-positions", {"code": code})
        if not data:
            continue
        positions = data if isinstance(data, list) else data.get("data", data.get("positions", []))
        rows = []
        for p in positions:
            if isinstance(p, dict):
                rows.append((
                    date, code,
                    p.get("institution_name", p.get("机构名称", "")),
                    p.get("shares", p.get("持股数", 0)),
                    p.get("change_shares", p.get("增减", 0)),
                    p.get("season", p.get("报告期", "")),
                ))
        if rows:
            n = store.insert_rows("stock_institutional_positions", rows,
                ["date", "stock_code", "institution_name", "shares", "change_shares", "season"])
            total += n
    if total:
        store.log_collect("stock_institutional_positions", "/stock/institutional-positions", total, "ok")
    return total


def collect_stock_holding_funds(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/stock/holding-funds", {"code": code})
        if not data:
            continue
        funds = data if isinstance(data, list) else data.get("data", data.get("funds", []))
        rows = []
        for f in funds:
            if isinstance(f, dict):
                rows.append((
                    date, code,
                    f.get("fund_name", f.get("基金名称", "")),
                    f.get("shares", f.get("持股数", 0)),
                    f.get("season", f.get("报告期", "")),
                ))
        if rows:
            n = store.insert_rows("stock_holding_funds", rows,
                ["date", "stock_code", "fund_name", "shares", "season"])
            total += n
    if total:
        store.log_collect("stock_holding_funds", "/stock/holding-funds", total, "ok")
    return total


def collect_stock_gudong(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    for code in stock_codes:
        data = client.get("/stock/gudong", {"code": code})
        if not data:
            continue
        shareholders = data if isinstance(data, list) else data.get("data", data.get("shareholders", []))
        rows = []
        for s in shareholders:
            if isinstance(s, dict):
                rows.append((
                    date, code,
                    s.get("shareholder_name", s.get("股东名称", "")),
                    s.get("shares", s.get("持股数", 0)),
                    s.get("ratio", s.get("比例", 0)),
                ))
        if rows:
            n = store.insert_rows("stock_gudong", rows,
                ["date", "stock_code", "shareholder_name", "shares", "ratio"])
            total += n
    if total:
        store.log_collect("stock_gudong", "/stock/gudong", total, "ok")
    return total


def collect_all_stock(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> dict:
    results = {}
    results["stock_company_info"] = collect_stock_company_info(client, store, date, stock_codes)
    results["stock_tags"] = collect_stock_tags(client, store, date, stock_codes)
    results["stock_institutional_positions"] = collect_stock_institutional_positions(client, store, date, stock_codes)
    results["stock_holding_funds"] = collect_stock_holding_funds(client, store, date, stock_codes)
    results["stock_gudong"] = collect_stock_gudong(client, store, date, stock_codes)
    return results
