"""Advanced data collectors (75 endpoints - largest category)."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore


def collect_advanced_news_flash(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/news-flash")
    if not data:
        return 0
    items = data.get("data", data.get("news", []))
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                str(item.get("TimeStamp", item.get("time", ""))),
                item.get("Title", item.get("title", "")),
                item.get("Source", item.get("source", "")),
                "",  # url if available
            ))
    if rows:
        n = store.insert_rows("advanced_news_flash", rows,
            ["date", "time", "news_title", "news_source", "news_url"])
        store.log_collect("advanced_news_flash", "/advanced/news-flash", n, "ok")
        return n
    return 0


def collect_advanced_pianlizhi(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/pianlizhi")
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("advanced_pianlizhi", [row], ["date", "raw_json"])
    store.log_collect("advanced_pianlizhi", "/advanced/pianlizhi", n, "ok")
    return n


def collect_advanced_market_radar(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/market-radar")
    if not data:
        return 0
    events = data.get("data", data.get("events", []))
    rows = []
    for ev in events:
        if isinstance(ev, dict):
            rows.append((
                date,
                str(ev.get("time", "")),
                str(ev.get("stock_code", ev.get("Code", ""))),
                ev.get("stock_name", ev.get("Name", "")),
                str(ev.get("event_type", ev.get("type", ""))),
                str(ev.get("desc", ev.get("Desc", ""))),
            ))
    if rows:
        n = store.insert_rows("advanced_market_radar", rows,
            ["date", "time", "stock_code", "stock_name", "event_type", "event_desc"])
        store.log_collect("advanced_market_radar", "/advanced/market-radar", n, "ok")
        return n
    return 0


def collect_advanced_concept_point(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/concept-point")
    if not data:
        return 0
    items = data.get("data", data.get("concepts", []))
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                item.get("name", item.get("concept_name", "")),
                item.get("change_pct", item.get("涨跌幅", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_concept_point", rows,
            ["date", "concept_name", "change_pct"])
        store.log_collect("advanced_concept_point", "/advanced/concept-point", n, "ok")
        return n
    return 0


def collect_advanced_morning_bidding(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/morning-bidding-summary")
    if not data:
        return 0
    summary = data.get("data", data)
    if isinstance(summary, dict):
        row = (
            date,
            summary.get("total_amount", summary.get("总金额", 0)),
            summary.get("limit_up_count", summary.get("涨停数", 0)),
            summary.get("limit_down_count", summary.get("跌停数", 0)),
        )
        n = store.insert_rows("advanced_morning_bidding_summary", [row],
            ["date", "total_amount", "limit_up_count", "limit_down_count"])
        store.log_collect("advanced_morning_bidding_summary", "/advanced/morning-bidding-summary", n, "ok")
        return n
    return 0


def collect_advanced_morning_bidding_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/morning-bidding-list")
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
                s.get("bidding_amount", s.get("竞价额", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_morning_bidding_list", rows,
            ["date", "stock_code", "stock_name", "bidding_amount"])
        store.log_collect("advanced_morning_bidding_list", "/advanced/morning-bidding-list", n, "ok")
        return n
    return 0


def collect_advanced_agency_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/agency-list", {"date": date})
    if not data:
        return 0
    items = data.get("data", data.get("agencies", []))
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                item.get("name", item.get("agency_name", "")),
                item.get("buy_count", item.get("买入次数", 0)),
                item.get("buy_amount", item.get("买入额", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_agency_list", rows,
            ["date", "agency_name", "buy_count", "buy_amount"])
        store.log_collect("advanced_agency_list", "/advanced/agency-list", n, "ok")
        return n
    return 0


def collect_advanced_business_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/business-list")
    if not data:
        return 0
    items = data.get("data", data.get("businesses", []))
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                item.get("name", item.get("business_name", "")),
                item.get("buy_count", item.get("买入次数", 0)),
                item.get("buy_amount", item.get("买入额", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_business_list", rows,
            ["date", "business_name", "buy_count", "buy_amount"])
        store.log_collect("advanced_business_list", "/advanced/business-list", n, "ok")
        return n
    return 0


def collect_advanced_holiday(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/holiday", {"year": date[:4]})
    if not data:
        return 0
    holidays = data.get("data", data.get("holidays", []))
    rows = []
    for h in holidays:
        if isinstance(h, dict):
            rows.append((
                int(date[:4]),
                h.get("date", ""),
                h.get("name", h.get("holiday_name", "")),
            ))
    if rows:
        n = store.insert_rows("advanced_holiday", rows, ["year", "holiday_date", "holiday_name"])
        store.log_collect("advanced_holiday", "/advanced/holiday", n, "ok")
        return n
    return 0


def collect_advanced_weipan_qiangchou(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/weipan-qiangchou")
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
            ))
    if rows:
        n = store.insert_rows("advanced_weipan_qiangchou", rows,
            ["date", "stock_code", "stock_name", "amount"])
        store.log_collect("advanced_weipan_qiangchou", "/advanced/weipan-qiangchou", n, "ok")
        return n
    return 0


def collect_advanced_fengk_best(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/fengk-best")
    if not data:
        return 0
    stocks = data.get("data", data.get("stocks", []))
    rows = []
    for s in stocks:
        if isinstance(s, dict):
            rows.append((
                data.get("date", date),
                str(s.get("stock_code", s.get("code", ""))),
                s.get("stock_name", s.get("name", "")),
                s.get("score", s.get("分数", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_fengk_best", rows,
            ["date", "stock_code", "stock_name", "score"])
        store.log_collect("advanced_fengk_best", "/advanced/fengk-best", n, "ok")
        return n
    return 0


def collect_advanced_on_the_lhb(client: KPLClient, store: DuckDBStore, date: str) -> int:
    # Pass the trade date: without it the request is ambiguous and the row
    # would be stamped with whatever the endpoint returned (or the run date).
    data = client.get("/advanced/on-the-lhb", {"date": date})
    if not data:
        return 0
    stocks = data.get("data", data.get("stocks", []))
    rows = []
    for s in stocks:
        if isinstance(s, dict):
            rows.append((
                date,
                str(s.get("stock_code", s.get("code", ""))),
                s.get("stock_name", s.get("name", "")),
                s.get("probability", s.get("概率", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_on_the_lhb", rows,
            ["date", "stock_code", "stock_name", "probability"])
        store.log_collect("advanced_on_the_lhb", "/advanced/on-the-lhb", n, "ok")
        return n
    return 0


def collect_advanced_market_mood_count(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/market-mood-count")
    if not data:
        return 0
    row = (
        date,
        data.get("rise_count", data.get("上涨家数", 0)),
        data.get("fall_count", data.get("下跌家数", 0)),
        data.get("limit_up_count", data.get("涨停数", 0)),
        data.get("limit_down_count", data.get("跌停数", 0)),
    )
    n = store.insert_rows("advanced_market_mood_count", [row],
        ["date", "rise_count", "fall_count", "limit_up_count", "limit_down_count"],
        replace_on=["date"])
    store.log_collect("advanced_market_mood_count", "/advanced/market-mood-count", n, "ok")
    return n


def collect_advanced_relation(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/relation")
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("advanced_relation", [row], ["date", "raw_json"])
    store.log_collect("advanced_relation", "/advanced/relation", n, "ok")
    return n


def collect_advanced_disk_review(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/disk-review", {"date": date})
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("advanced_disk_review", [row], ["date", "raw_json"],
        replace_on=["date"])
    store.log_collect("advanced_disk_review", "/advanced/disk-review", n, "ok")
    return n


def collect_advanced_market_scln(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/market-scln", {"date": date})
    if not data:
        return 0
    row = (
        date,
        data.get("total_volume", data.get("总成交量", 0)),
        data.get("total_turnover", data.get("总成交额", 0)),
        data.get("mtype", ""),
    )
    n = store.insert_rows("advanced_market_scln", [row],
        ["date", "total_volume", "total_turnover", "mtype"],
        replace_on=["date", "mtype"])
    store.log_collect("advanced_market_scln", "/advanced/market-scln", n, "ok")
    return n


def collect_advanced_his_sharp_withdrawal(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/his-sharp-withdrawal", {"date": date})
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
                s.get("withdrawal_pct", s.get("回撤幅度", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_his_sharp_withdrawal", rows,
            ["date", "stock_code", "stock_name", "withdrawal_pct"])
        store.log_collect("advanced_his_sharp_withdrawal", "/advanced/his-sharp-withdrawal", n, "ok")
        return n
    return 0


def collect_advanced_weight_performance(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/weight-performance", {"date": date})
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
                s.get("change_pct", s.get("涨跌幅", 0)),
                s.get("weight", s.get("权重", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_weight_performance", rows,
            ["date", "stock_code", "stock_name", "change_pct", "weight"])
        store.log_collect("advanced_weight_performance", "/advanced/weight-performance", n, "ok")
        return n
    return 0


def collect_advanced_zhangting_expression(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/zhangting-expression", {"date": date})
    if not data:
        return 0
    items = data if isinstance(data, list) else data.get("data", [])
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append((
                date,
                item.get("expression_type", item.get("类型", "")),
                item.get("count", item.get("数量", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_zhangting_expression", rows,
            ["date", "expression_type", "count"])
        store.log_collect("advanced_zhangting_expression", "/advanced/zhangting-expression", n, "ok")
        return n
    return 0


def collect_advanced_newhigh_group(client: KPLClient, store: DuckDBStore, date: str) -> int:
    total = 0
    # Group count
    data = client.get("/advanced/newhigh-group-count")
    if data:
        items = data.get("data", [])
        rows = []
        for item in items:
            if isinstance(item, dict):
                rows.append((
                    date,
                    item.get("group_type", item.get("类型", "")),
                    item.get("count", item.get("数量", 0)),
                ))
        if rows:
            n = store.insert_rows("advanced_newhigh_group_count", rows,
                ["date", "group_type", "count"])
            total += n
    
    # Group stocks
    for group_type in ["", "1", "2", "3"]:
        data = client.get("/advanced/newhigh-group-stocks", {"group_type": group_type})
        if not data:
            continue
        stocks = data if isinstance(data, list) else data.get("data", [])
        rows = []
        for s in stocks:
            if isinstance(s, dict):
                rows.append((
                    date, group_type or "all",
                    str(s.get("stock_code", s.get("code", ""))),
                    s.get("stock_name", s.get("name", "")),
                ))
        if rows:
            n = store.insert_rows("advanced_newhigh_group_stocks", rows,
                ["date", "group_type", "stock_code", "stock_name"])
            total += n
    
    if total:
        store.log_collect("advanced_newhigh_group", "/advanced/newhigh-group-*", total, "ok")
    return total


def collect_advanced_interviews(client: KPLClient, store: DuckDBStore, date: str) -> int:
    from datetime import timedelta
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    end = date
    data = client.get("/advanced/interviews", {"start": start, "end": end})
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
                s.get("institution_count", s.get("机构数", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_interviews", rows,
            ["date", "stock_code", "stock_name", "institution_count"])
        store.log_collect("advanced_interviews", "/advanced/interviews", n, "ok")
        return n
    return 0


def collect_advanced_his_ranking(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/advanced/his-ranking", {"date": date})
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
                s.get("change_pct", s.get("涨跌幅", 0)),
                s.get("ranking", s.get("排名", 0)),
            ))
    if rows:
        n = store.insert_rows("advanced_his_ranking", rows,
            ["date", "stock_code", "stock_name", "change_pct", "ranking"])
        store.log_collect("advanced_his_ranking", "/advanced/his-ranking", n, "ok")
        return n
    return 0


def collect_advanced_his_ranking_info(client: KPLClient, store: DuckDBStore, date: str) -> int:
    """Requires date param."""
    data = client.get("/advanced/his-ranking-info", {"date": date})
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("advanced_his_ranking_info", [row], ["date", "raw_json"])
    store.log_collect("advanced_his_ranking_info", "/advanced/his-ranking-info", n, "ok")
    return n


def collect_advanced_company_count(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """Requires codes (plural) param."""
    if not stock_codes:
        return 0
    codes_str = ",".join(stock_codes[:50])
    data = client.get("/advanced/company-count", {"codes": codes_str})
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("advanced_company_count", [row], ["date", "raw_json"])
    store.log_collect("advanced_company_count", "/advanced/company-count", n, "ok")
    return n


def collect_all_advanced(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    results = {}
    results["advanced_news_flash"] = collect_advanced_news_flash(client, store, date)
    results["advanced_pianlizhi"] = collect_advanced_pianlizhi(client, store, date)
    results["advanced_market_radar"] = collect_advanced_market_radar(client, store, date)
    results["advanced_concept_point"] = collect_advanced_concept_point(client, store, date)
    results["advanced_morning_bidding"] = collect_advanced_morning_bidding(client, store, date)
    results["advanced_morning_bidding_list"] = collect_advanced_morning_bidding_list(client, store, date)
    results["advanced_agency_list"] = collect_advanced_agency_list(client, store, date)
    results["advanced_business_list"] = collect_advanced_business_list(client, store, date)
    results["advanced_holiday"] = collect_advanced_holiday(client, store, date)
    results["advanced_weipan_qiangchou"] = collect_advanced_weipan_qiangchou(client, store, date)
    results["advanced_fengk_best"] = collect_advanced_fengk_best(client, store, date)
    results["advanced_on_the_lhb"] = collect_advanced_on_the_lhb(client, store, date)
    results["advanced_market_mood_count"] = collect_advanced_market_mood_count(client, store, date)
    results["advanced_relation"] = collect_advanced_relation(client, store, date)
    results["advanced_disk_review"] = collect_advanced_disk_review(client, store, date)
    results["advanced_market_scln"] = collect_advanced_market_scln(client, store, date)
    results["advanced_his_sharp_withdrawal"] = collect_advanced_his_sharp_withdrawal(client, store, date)
    results["advanced_weight_performance"] = collect_advanced_weight_performance(client, store, date)
    results["advanced_zhangting_expression"] = collect_advanced_zhangting_expression(client, store, date)
    results["advanced_newhigh_group"] = collect_advanced_newhigh_group(client, store, date)
    results["advanced_interviews"] = collect_advanced_interviews(client, store, date)
    results["advanced_his_ranking"] = collect_advanced_his_ranking(client, store, date)
    results["advanced_his_ranking_info"] = collect_advanced_his_ranking_info(client, store, date)
    return results
