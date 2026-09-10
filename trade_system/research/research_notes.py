"""Research note persistence for operator review."""

from __future__ import annotations

import hashlib
from pathlib import Path


from trade_system.research.schema import ensure_research_tables


def _note_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def upsert_research_note(
    db_path: str | Path,
    *,
    trade_date: str,
    symbol: str,
    sector: str,
    theme: str,
    note_type: str,
    content: str,
    evidence_refs: str = "",
) -> str:
    ensure_research_tables(db_path)
    note_id = _note_id(trade_date, symbol, sector, theme, note_type, content)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute("DELETE FROM research_note WHERE note_id = ?", [note_id])
        con.execute(
            """
            INSERT INTO research_note (
                note_id, trade_date, symbol, sector, theme, note_type, content, evidence_refs
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [note_id, trade_date, symbol, sector, theme, note_type, content, evidence_refs],
        )
        return note_id
    finally:
        con.close()
