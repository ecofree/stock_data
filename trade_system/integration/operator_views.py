"""Unified operator-facing views over native and imported legacy data."""

from __future__ import annotations

from pathlib import Path

import duckdb


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name = ?",
        [name],
    ).fetchone()
    if row and row[0]:
        return True
    row = con.execute(
        "SELECT count(*) FROM information_schema.views WHERE table_schema='main' AND table_name = ?",
        [name],
    ).fetchone()
    return bool(row and row[0])


def _empty_candidates_sql() -> str:
    return """
    SELECT
        CAST(NULL AS VARCHAR) AS trade_date,
        CAST(NULL AS VARCHAR) AS stage,
        CAST(NULL AS VARCHAR) AS stock_code,
        CAST(NULL AS VARCHAR) AS stock_name,
        CAST(NULL AS DOUBLE) AS score,
        CAST(NULL AS VARCHAR) AS decision,
        CAST(NULL AS VARCHAR) AS data_origin
    WHERE false
    """


def build_operator_views(db_path: str | Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        native_columns = (
            {row[1] for row in con.execute("PRAGMA table_info('stock_candidate_stage_signal')").fetchall()}
            if _relation_exists(con, "stock_candidate_stage_signal")
            else set()
        )
        actionable_filter = (
            "WHERE coalesce(is_actionable, false) = true"
            if "is_actionable" in native_columns
            else ""
        )
        native = (
            f"""
            SELECT
                CAST(trade_date AS VARCHAR) AS trade_date,
                stage,
                stock_code,
                stock_name,
                score,
                decision,
                'stock_data' AS data_origin
            FROM stock_candidate_stage_signal
            {actionable_filter}
            """
            if _relation_exists(con, "stock_candidate_stage_signal")
            else _empty_candidates_sql()
        )
        legacy = (
            """
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'legacy_watchlist' AS stage,
                stock_code,
                stock_name,
                final_score AS score,
                'legacy_reference' AS decision,
                'legacy_qds' AS data_origin
            FROM legacy_qds_daily_watchlist
            """
            if _relation_exists(con, "legacy_qds_daily_watchlist")
            else _empty_candidates_sql()
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_operator_candidates AS
            SELECT * FROM ({native})
            UNION ALL
            SELECT * FROM ({legacy})
            """
        )
    finally:
        con.close()
