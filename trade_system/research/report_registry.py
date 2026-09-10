"""Research report file registry for review and attribution."""

from __future__ import annotations

import hashlib
from pathlib import Path


from trade_system.research.schema import ensure_research_tables


def _report_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def upsert_research_report(
    db_path: str | Path,
    *,
    symbol: str,
    title: str,
    publisher: str,
    report_date: str,
    file_path: str,
    source_url: str,
    summary: str,
    risk_tags: str = "",
) -> str:
    ensure_research_tables(db_path)
    report_id = _report_id(symbol, title, publisher, report_date, file_path)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute("DELETE FROM research_report_file WHERE report_id = ?", [report_id])
        con.execute(
            """
            INSERT INTO research_report_file (
                report_id, symbol, title, publisher, report_date, file_path, source_url, summary, risk_tags
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [report_id, symbol, title, publisher, report_date, file_path, source_url, summary, risk_tags],
        )
        return report_id
    finally:
        con.close()
