"""News and topic data collectors (15 endpoints)."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore, logger


def collect_news_plate(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:30]:
        data = client.get("/news/plate", {"code": code})
        if not data:
            continue
        news = data if isinstance(data, list) else data.get("data", data.get("news", []))
        rows = []
        for n in news:
            if isinstance(n, dict):
                rows.append((
                    date, code,
                    n.get("title", n.get("新闻标题", "")),
                    n.get("url", n.get("链接", "")),
                    n.get("source", n.get("来源", "")),
                ))
        if rows:
            n = store.insert_rows("news_plate", rows,
                ["date", "sector_code", "news_title", "news_url", "news_source"])
            total += n
    if total:
        store.log_collect("news_plate", "/news/plate", total, "ok")
    return total


def collect_news_columns(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/news/columns")
    if not data:
        return 0
    columns = data.get("columns", data.get("data", []))
    rows = []
    for c in columns:
        if isinstance(c, dict):
            rows.append((
                str(c.get("id", c.get("column_id", ""))),
                c.get("name", c.get("column_name", "")),
            ))
    if rows:
        n = store.insert_rows("news_columns", rows, ["column_id", "column_name"])
        store.log_collect("news_columns", "/news/columns", n, "ok")
        return n
    store.insert_raw("/news/columns", data)
    return 0


def collect_news_concept_jxbk(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/news/concept-jxbk")
    if not data:
        return 0
    concepts = data if isinstance(data, list) else data.get("data", data.get("concepts", []))
    rows = []
    for c in concepts:
        if isinstance(c, dict):
            rows.append((
                str(c.get("code", c.get("sector_code", ""))),
                c.get("name", c.get("sector_name", "")),
            ))
    if rows:
        n = store.insert_rows("news_concept_jxbk", rows, ["sector_code", "sector_name"])
        store.log_collect("news_concept_jxbk", "/news/concept-jxbk", n, "ok")
        return n
    store.insert_raw("/news/concept-jxbk", data)
    return 0


def collect_topic_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    total = 0
    for idx in range(0, 100, 20):
        data = client.get("/topic/list", {"index": str(idx), "page_size": "20"})
        if not data:
            break
        topics = data if isinstance(data, list) else data.get("data", data.get("topics", []))
        if not topics:
            break
        rows = []
        for t in topics:
            if isinstance(t, dict):
                rows.append((
                    date,
                    str(t.get("id", t.get("topic_id", ""))),
                    t.get("title", t.get("topic_title", "")),
                    t.get("heat", t.get("热度", 0)),
                ))
        if rows:
            n = store.insert_rows("topic_list", rows,
                ["date", "topic_id", "topic_title", "heat_score"])
            total += n
        if len(topics) < 20:
            break
    if total:
        store.log_collect("topic_list", "/topic/list", total, "ok")
    return total


def collect_news_theme(client: KPLClient, store: DuckDBStore, date: str) -> int:
    total = 0
    for idx in range(0, 100, 20):
        data = client.get("/news/theme", {"type": "-1", "index": str(idx), "page_size": "20"})
        if not data:
            break
        news = data if isinstance(data, list) else data.get("data", data.get("news", []))
        if not news:
            break
        rows = []
        for n in news:
            if isinstance(n, dict):
                rows.append((
                    date,
                    n.get("title", n.get("新闻标题", "")),
                    n.get("url", n.get("链接", "")),
                    n.get("theme_name", n.get("题材名称", "")),
                ))
        if rows:
            n = store.insert_rows("news_theme", rows,
                ["date", "news_title", "news_url", "theme_name"])
            total += n
        if len(news) < 20:
            break
    if total:
        store.log_collect("news_theme", "/news/theme", total, "ok")
    return total


def collect_news_index_plate(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/news/index-plate")
    if not data:
        return 0
    plates = data if isinstance(data, list) else data.get("data", data.get("plates", []))
    rows = []
    for p in plates:
        if isinstance(p, dict):
            rows.append((
                date,
                str(p.get("code", p.get("sector_code", ""))),
                p.get("name", p.get("sector_name", "")),
            ))
    if rows:
        n = store.insert_rows("news_index_plate", rows,
            ["date", "sector_code", "sector_name"])
        store.log_collect("news_index_plate", "/news/index-plate", n, "ok")
        return n
    store.insert_raw("/news/index-plate", data)
    return 0


def collect_all_news(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> dict:
    results = {}
    results["news_columns"] = collect_news_columns(client, store, date)
    results["news_concept_jxbk"] = collect_news_concept_jxbk(client, store, date)
    results["topic_list"] = collect_topic_list(client, store, date)
    results["news_theme"] = collect_news_theme(client, store, date)
    results["news_index_plate"] = collect_news_index_plate(client, store, date)
    results["news_plate"] = collect_news_plate(client, store, date, sector_codes)
    return results
