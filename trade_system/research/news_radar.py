"""Normalize and persist objective news-radar items."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


from trade_system.research.schema import ensure_research_tables


NEWS_COLUMNS = [
    "news_id",
    "trade_date",
    "source",
    "title",
    "url",
    "published_at",
    "industry",
    "related_sector",
    "related_symbol",
    "keywords",
    "risk_tags",
    "catalyst_tags",
    "summary",
    "raw_payload_hash",
]


RISK_KEYWORDS = ["减持", "监管", "处罚", "亏损", "暴雷", "立案", "退市"]


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_radar_items(radar: dict[str, Any], trade_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for industry in radar.get("industries", []):
        sector_key = industry.get("key") or ""
        industry_name = industry.get("name") or sector_key
        for item in industry.get("items", []):
            title = item.get("title", "")
            summary = item.get("summary", "")
            payload = {
                "trade_date": trade_date,
                "source": item.get("source", ""),
                "title": title,
                "url": item.get("url", ""),
            }
            raw_hash = _stable_hash(item)
            risk_tags = ",".join(keyword for keyword in RISK_KEYWORDS if keyword in f"{title} {summary}")
            rows.append(
                {
                    "news_id": _stable_hash(payload)[:24],
                    "trade_date": trade_date,
                    "source": item.get("source", ""),
                    "title": title,
                    "url": item.get("url", ""),
                    "published_at": item.get("time", ""),
                    "industry": industry_name,
                    "related_sector": sector_key,
                    "related_symbol": item.get("symbol"),
                    "keywords": item.get("keywords", title),
                    "risk_tags": risk_tags,
                    "catalyst_tags": industry_name,
                    "summary": summary,
                    "raw_payload_hash": raw_hash,
                }
            )
    return rows


def upsert_news_items(db_path: str | Path, rows: list[dict[str, Any]]) -> int:
    ensure_research_tables(db_path)
    if not rows:
        return 0
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        for row in rows:
            con.execute("DELETE FROM news_radar_item WHERE news_id = ?", [row["news_id"]])
            values = [row.get(col) for col in NEWS_COLUMNS]
            placeholders = ", ".join(["?"] * len(NEWS_COLUMNS))
            column_sql = ", ".join(f'"{col}"' for col in NEWS_COLUMNS)
            con.execute(f"INSERT INTO news_radar_item ({column_sql}) VALUES ({placeholders})", values)
        return len(rows)
    finally:
        con.close()
