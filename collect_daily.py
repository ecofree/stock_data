"""Daily data collectors (4 endpoints)."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore, logger


def collect_daily(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/daily", {"date": date})
    if not data:
        return 0
    try:
        row = (
            date,
            data.get("limit_up_count", data.get("涨停数", 0)),
            data.get("limit_down_count", data.get("跌停数", 0)),
            data.get("rise_count", data.get("上涨家数", 0)),
            data.get("fall_count", data.get("下跌家数", 0)),
            data.get("consecutive_count", data.get("连板数", 0)),
            json.dumps(data, ensure_ascii=False),
        )
        # A derived fallback may already occupy the date primary key.  A real
        # KPL retry must replace it, not fail with a duplicate-key error.
        store.conn.execute(
            """
            INSERT INTO daily_summary
            (date,limit_up_count,limit_down_count,rise_count,fall_count,
             consecutive_count,raw_json,fetched_at,source_kind)
            VALUES (?,?,?,?,?,?,?,current_timestamp,'real')
            ON CONFLICT(date) DO UPDATE SET
                limit_up_count=excluded.limit_up_count,
                limit_down_count=excluded.limit_down_count,
                rise_count=excluded.rise_count,
                fall_count=excluded.fall_count,
                consecutive_count=excluded.consecutive_count,
                raw_json=excluded.raw_json,
                fetched_at=excluded.fetched_at,
                source_kind='real'
            """,
            list(row),
        )
        n = 1
        store.log_collect("daily_summary", "/daily", n, "ok")
        return n
    except Exception as e:
        logger.error(f"daily error: {e}")
        store.insert_raw("/daily", data)
        return 0


def collect_daily_new_high(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/daily/new-high", {"date": date})
    if not data:
        return 0
    count = data.get("count", 0)
    row = (date, count)
    n = store.insert_rows("daily_new_high", [row], ["date", "count"])
    store.log_collect("daily_new_high", "/daily/new-high", n, "ok")
    return n


def collect_daily_sentiment(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/daily/sentiment", {"date": date})
    if not data:
        return 0
    try:
        if isinstance(data, list):
            data_dict = data[0] if data and isinstance(data[0], dict) else {}
        else:
            data_dict = data
        
        # API returns Chinese field names
        limit_up = data_dict.get("涨停数", data_dict.get("sentiment_score", 0))
        row = (date, limit_up, json.dumps(data, ensure_ascii=False))
        n = store.insert_rows("daily_sentiment", [row], ["date", "sentiment_score", "raw_json"])
        store.log_collect("daily_sentiment", "/daily/sentiment", n, "ok")
        return n
    except Exception as e:
        logger.error(f"daily_sentiment parse error: {e}")
        store.insert_raw("/daily/sentiment", data)
        return 0


def collect_daily_export(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/daily/export", {"date": date})
    if not data:
        return 0
    row = (date, json.dumps(data, ensure_ascii=False))
    n = store.insert_rows("daily_export", [row], ["date", "raw_json"])
    store.log_collect("daily_export", "/daily/export", n, "ok")
    return n


def collect_all_daily(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    return {
        "daily": collect_daily(client, store, date),
        "daily_new_high": collect_daily_new_high(client, store, date),
        "daily_sentiment": collect_daily_sentiment(client, store, date),
        "daily_export": collect_daily_export(client, store, date),
    }
