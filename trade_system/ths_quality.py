"""Shared quality contract for THS concept snapshots."""

from __future__ import annotations

from datetime import date
from typing import Any


# The expected catalogue size is source-controlled per snapshot.  A global
# floor would silently accept a truncated catalogue after the source changed.
THS_MIN_CONCEPTS: int | None = None


def _table_exists(con: Any, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0])


def canonical_ths_snapshot(
    con: Any,
    as_of: str,
    *,
    exact_date: str | None = None,
    minimum_concepts: int | None = THS_MIN_CONCEPTS,
) -> dict[str, Any] | None:
    """Return the newest globally complete, same-date THS snapshot.

    A successful transport/checkpoint is not enough.  Every concept and member
    row must be date-verified, all rows must share one fetched date, every
    member checkpoint must succeed, and the catalogue must equal the expected
    count recorded for that snapshot.  This deliberately returns no fallback snapshot: callers
    must show an explicit unavailable/degraded state instead of mixing dates.
    """
    required = (
        "ths_concept_daily",
        "ths_concept_stock_history",
        "ths_concept_member_checkpoint",
        "ths_concept_snapshot_expectation",
    )
    if any(not _table_exists(con, name) for name in required):
        return None
    params: list[Any] = [as_of]
    date_filter = ""
    if exact_date:
        date_filter = " AND c.trade_date=CAST(? AS DATE)"
        params.append(exact_date)
    minimum_filter = " AND c.concept_count>=?" if minimum_concepts is not None else ""
    if minimum_concepts is not None:
        params.append(int(minimum_concepts))
    row = con.execute(
        f"""
        WITH concepts AS (
            SELECT c.trade_date,
                   count(DISTINCT c.concept_code) AS concept_count,
                   count(*) AS concept_rows,
                   sum(CASE WHEN coalesce(c.date_verified,false) THEN 1 ELSE 0 END) AS verified_concepts,
                   count(DISTINCT CASE WHEN json_valid(c.raw_json)
                       THEN json_extract_string(c.raw_json,'$.fetched_date') END) AS fetched_dates,
                   sum(CASE WHEN coalesce(c.stock_count,0)=0 THEN 1 ELSE 0 END) AS zero_concepts
            FROM ths_concept_daily c
            WHERE c.trade_date<=CAST(? AS DATE){date_filter}
            GROUP BY c.trade_date
        ), members AS (
            SELECT h.trade_date,
                   count(*) AS member_rows,
                   sum(CASE WHEN coalesce(h.date_verified,false) THEN 1 ELSE 0 END) AS verified_members,
                   count(DISTINCT CASE WHEN json_valid(h.raw_json)
                       THEN json_extract_string(h.raw_json,'$.fetched_date') END) AS fetched_dates
            FROM ths_concept_stock_history h
            GROUP BY h.trade_date
        ), checkpoints AS (
            SELECT trade_date,
                   count(DISTINCT concept_code) AS checkpoint_total,
                   sum(CASE WHEN status='success' THEN 1 ELSE 0 END) AS checkpoint_success
            FROM ths_concept_member_checkpoint
            GROUP BY trade_date
        ), expected AS (
            SELECT trade_date, expected_concepts
            FROM ths_concept_snapshot_expectation
            WHERE status IN ('success', 'bootstrap_observed')
        )
        SELECT CAST(c.trade_date AS VARCHAR), c.concept_count, m.member_rows,
               c.fetched_dates, m.fetched_dates, c.verified_concepts,
               m.verified_members, c.zero_concepts, cp.checkpoint_total,
               cp.checkpoint_success, e.expected_concepts
        FROM concepts c
        JOIN members m ON m.trade_date=c.trade_date
        JOIN checkpoints cp ON cp.trade_date=c.trade_date
        JOIN expected e ON e.trade_date=c.trade_date
        WHERE c.concept_count=e.expected_concepts{minimum_filter}
          AND c.fetched_dates=1 AND m.fetched_dates=1
          AND c.verified_concepts=c.concept_rows
          AND m.verified_members=m.member_rows
          AND c.zero_concepts=0
          AND cp.checkpoint_total=c.concept_count
          AND cp.checkpoint_success=c.concept_count
          AND m.member_rows>c.concept_count
        ORDER BY c.trade_date DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    snapshot_date = str(row[0])[:10]
    return {
        "snapshot_date": snapshot_date,
        "concepts": int(row[1] or 0),
        "members": int(row[2] or 0),
        "fetched_dates": int(row[3] or 0),
        "member_fetched_dates": int(row[4] or 0),
        "verified_concepts": int(row[5] or 0),
        "verified_members": int(row[6] or 0),
        "zero_concepts": int(row[7] or 0),
        "checkpoint_total": int(row[8] or 0),
        "checkpoint_success": int(row[9] or 0),
        "age_days": (date.fromisoformat(as_of) - date.fromisoformat(snapshot_date)).days,
    }
