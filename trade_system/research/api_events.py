"""Import KPL API event evidence into the research layer.

The adapter intentionally stores compact, deduplicated event metadata only.
Raw API responses stay in their source tables and are not copied into reports.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb

from trade_system.normalize import build_normalized_views
from trade_system.research.news_radar import upsert_news_items
from trade_system.research.schema import ensure_research_tables


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fetch_api_events(db_path: str | Path, trade_date: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        where = "WHERE source_table <> 'news_radar_item'"
        params: list[Any] = []
        if trade_date:
            where += " AND trade_date = ?"
            params.append(trade_date)
        rows = con.execute(
            f"""
            SELECT
                trade_date,
                event_type,
                symbol,
                sector_code,
                title,
                source,
                url,
                risk_tags,
                catalyst_tags,
                source_table
            FROM v_research_event_evidence
            {where}
            ORDER BY trade_date DESC NULLS LAST, source_table, title
            LIMIT {int(limit)}
            """,
            params,
        ).fetchall()
    finally:
        con.close()
    columns = [
        "trade_date",
        "event_type",
        "symbol",
        "sector_code",
        "title",
        "source",
        "url",
        "risk_tags",
        "catalyst_tags",
        "source_table",
    ]
    return [dict(zip(columns, row)) for row in rows]


def _to_news_radar_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for event in events:
        title = event.get("title")
        trade_date = event.get("trade_date")
        if not title or not trade_date:
            continue
        identity = {
            "trade_date": trade_date,
            "source_table": event.get("source_table"),
            "event_type": event.get("event_type"),
            "symbol": event.get("symbol"),
            "sector": event.get("sector_code"),
            "title": title,
        }
        raw_hash = _stable_hash(identity)
        rows.append(
            {
                "news_id": f"kpl_api_{raw_hash[:24]}",
                "trade_date": trade_date,
                "source": event.get("source") or event.get("source_table") or "kpl_api",
                "title": title,
                "url": event.get("url"),
                "published_at": trade_date,
                "industry": event.get("sector_code"),
                "related_sector": event.get("sector_code"),
                "related_symbol": event.get("symbol"),
                "keywords": title,
                "risk_tags": event.get("risk_tags"),
                "catalyst_tags": event.get("catalyst_tags") or event.get("event_type"),
                "summary": (
                    f"event_type={event.get('event_type')}; "
                    f"source_table={event.get('source_table')}; "
                    "direct_signal=false"
                ),
                "raw_payload_hash": raw_hash,
            }
        )
    return rows


def import_api_research_events(db_path: str | Path, trade_date: str | None = None, limit: int = 1000) -> int:
    ensure_research_tables(db_path)
    build_normalized_views(db_path)
    events = _fetch_api_events(db_path, trade_date=trade_date, limit=limit)
    rows = _to_news_radar_rows(events)
    return upsert_news_items(db_path, rows)
