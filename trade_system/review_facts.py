"""Facts needed by the detailed post-market review page.

The HTML renderer must remain a presentation layer.  This module owns the
small set of page-specific historical queries that are not part of the shared
daily-review context and returns one stable payload for the renderer.
"""

from __future__ import annotations

import json
from typing import Any

import duckdb

from trade_system.quality import table_exists


def _trend_series(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    trend: dict[str, Any] = {
        "dates": [], "limit_up": [], "limit_down": [], "broken_rate": [],
        "rise": [], "fall": [], "emotion": [], "consecutive": [],
    }
    try:
        rows = con.execute(
            """
            WITH latest_r AS (
                SELECT * FROM market_rise_fall
                QUALIFY row_number() OVER (PARTITION BY date ORDER BY updated_at DESC) = 1
            ), latest_s AS (
                SELECT * FROM daily_summary
                QUALIFY row_number() OVER (PARTITION BY date ORDER BY fetched_at DESC) = 1
            ), latest_e AS (
                SELECT * FROM market_emotion_money
                QUALIFY row_number() OVER (PARTITION BY date ORDER BY fetched_at DESC) = 1
            )
            SELECT r.date, r.limit_up_count, r.limit_down_count,
                   r.broken_limit_up_count, r.blown_limit_up_rate,
                   s.rise_count, s.fall_count, s.consecutive_count,
                   e.cgl
            FROM latest_r r
            LEFT JOIN latest_s s USING (date)
            LEFT JOIN latest_e e USING (date)
            WHERE r.date <= CAST(? AS DATE)
            ORDER BY r.date DESC
            LIMIT 30
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        rows = []
    for row in reversed(rows):
        trend["dates"].append(str(row[0]))
        trend["limit_up"].append(row[1])
        trend["limit_down"].append(row[2])
        trend["broken_rate"].append(round(float(row[4]), 2) if row[4] is not None else None)
        trend["rise"].append(row[5])
        trend["fall"].append(row[6])
        trend["consecutive"].append(row[7])
        trend["emotion"].append(round(float(row[8]), 2) if row[8] is not None else None)
    return trend


def _limit_ladder(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    """Return a same-date board-height distribution, preferring named boards."""
    if table_exists(con, "v_limit_pool"):
        try:
            live = con.execute(
                """
                SELECT board_level, count(*) FROM v_limit_pool
                WHERE CAST(trade_date AS DATE) = CAST(? AS DATE)
                GROUP BY board_level ORDER BY board_level
                """,
                [trade_date],
            ).fetchall()
            if live:
                return [{"height": int(row[0]), "count": int(row[1])} for row in live if row[0] is not None]
        except Exception:
            pass
    if not table_exists(con, "ladder_market"):
        return []
    try:
        columns = [row[0] for row in con.execute("DESCRIBE ladder_market").fetchall()]
    except Exception:
        columns = []
    if "consecutive_days" in columns:
        try:
            rows = con.execute(
                """
                SELECT consecutive_days, count(*) FROM ladder_market
                WHERE date = CAST(? AS DATE)
                GROUP BY consecutive_days ORDER BY consecutive_days
                """,
                [trade_date],
            ).fetchall()
            if rows:
                return [{"height": int(row[0]), "count": int(row[1])} for row in rows if row[0] is not None]
            if table_exists(con, "l2_realtime_all_boards"):
                live = con.execute(
                    """
                    SELECT board_level, count(*) FROM l2_realtime_all_boards
                    WHERE date = CAST(? AS DATE)
                    GROUP BY board_level ORDER BY board_level
                    """,
                    [trade_date],
                ).fetchall()
                return [{"height": int(row[0]), "count": int(row[1])} for row in live if row[0] is not None]
            return []
        except Exception:
            return []
    try:
        rows = con.execute(
            """
            SELECT raw_json FROM ladder_market
            WHERE date = CAST(? AS DATE)
            ORDER BY fetched_at DESC LIMIT 1
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    if not rows:
        return []
    try:
        payload = json.loads(rows[0][0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    ladder = payload.get("ladder") if isinstance(payload, dict) else None
    result: list[dict[str, Any]] = []
    if isinstance(ladder, list):
        for item in ladder:
            if not isinstance(item, dict):
                continue
            height = item.get("height") or item.get("height_count") or item.get("board_count")
            count = item.get("count") or item.get("total")
            if height is not None and count is not None:
                result.append({"height": int(height), "count": int(count)})
    if not result and isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"date", "is_realtime", "statistics", "broken_stocks", "height_marks"}:
                continue
            try:
                result.append({"height": int(key), "count": int(value)})
            except (TypeError, ValueError):
                continue
    return sorted(result, key=lambda item: item["height"])


def _theme_mainline(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    if not table_exists(con, "v_theme_mainline_evidence"):
        return []
    try:
        rows = con.execute(
            """
            SELECT sector_code, sector_name, strength_value, limit_up_count, seal_rate,
                   main_net_inflow, component_count, boom_reason, mainline_score
            FROM v_theme_mainline_evidence
            WHERE CAST(trade_date AS DATE) = (
                SELECT max(CAST(trade_date AS DATE)) FROM v_theme_mainline_evidence
                WHERE CAST(trade_date AS DATE) <= ?
            )
            ORDER BY mainline_score DESC NULLS LAST
            LIMIT 12
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "code": str(row[0]), "name": str(row[1] or ""),
            "strength": row[2], "limit_up_count": row[3], "seal_rate": row[4],
            "main_net": row[5], "components": row[6],
            "reason": row[7], "score": row[8],
        }
        for row in rows
    ]


def _sector_rotation(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    if not table_exists(con, "sector_rotation_score"):
        return []
    try:
        rows = con.execute(
            """
            SELECT sector_name, score, strength_value, limit_up_count, seal_rate
            FROM sector_rotation_score
            WHERE trade_date = CAST(? AS DATE)
              AND (taxonomy IN ('ths_concept', 'ths_concept_derived')
                   OR (coalesce(taxonomy, 'unknown') = 'unknown' AND sector_code LIKE 'THS-%'))
            ORDER BY score DESC LIMIT 12
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    return [
        {"name": str(row[0] or ""), "score": row[1], "strength": row[2],
         "limit_up": row[3], "seal_rate": row[4]}
        for row in rows
    ]


def _candidate_flow(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    try:
        rows = con.execute(
            """
            SELECT stage,
                   count(*) AS total,
                   sum(CASE WHEN is_actionable THEN 1 ELSE 0 END) AS actionable,
                   sum(CASE WHEN tradable THEN 1 ELSE 0 END) AS tradable,
                   sum(CASE WHEN risk_approved THEN 1 ELSE 0 END) AS approved,
                   sum(CASE WHEN is_executable THEN 1 ELSE 0 END) AS executable
            FROM stock_candidate_stage_signal
            WHERE trade_date = CAST(? AS DATE)
            GROUP BY stage ORDER BY stage
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    order = {"premarket_pool": 0, "auction_confirmation": 1, "intraday_strength": 2, "close_decision": 3}
    return [
        {"stage": str(row[0]), "total": row[1], "actionable": row[2],
         "tradable": row[3], "approved": row[4], "executable": row[5]}
        for row in sorted(rows, key=lambda item: order.get(str(item[0]), 99))
    ]


def build_review_page_facts(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """Build all page-only facts in one read-only database boundary."""
    facts = {
        "trend": _trend_series(con, trade_date),
        "ladder": _limit_ladder(con, trade_date),
        "rotation": _sector_rotation(con, trade_date),
        "theme_mainline": _theme_mainline(con, trade_date),
        "candidate_flow": _candidate_flow(con, trade_date),
        "emotion_rows": [],
        "consecutive": None,
    }
    try:
        facts["emotion_rows"] = [
            {"cgl": row[0]}
            for row in con.execute(
                "SELECT cgl FROM market_emotion_money WHERE date=CAST(? AS DATE) LIMIT 1",
                [trade_date],
            ).fetchall()
        ]
    except Exception:
        pass
    # The shared context may already have a reliable same-date ladder.  The
    # fallback queries below remain isolated here instead of the renderer.
    try:
        if table_exists(con, "ladder_market"):
            row = con.execute(
                "SELECT max(consecutive_days) FROM ladder_market WHERE date=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
            facts["consecutive"] = row[0] if row else None
    except Exception:
        pass
    if facts["consecutive"] is None and table_exists(con, "l2_realtime_all_boards"):
        try:
            row = con.execute(
                "SELECT max(board_level) FROM l2_realtime_all_boards WHERE date=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
            facts["consecutive"] = row[0] if row else None
        except Exception:
            pass
    return facts
