"""Canonical multi-horizon capital-flow features for research and review.

The collectors retain every provider row for audit.  This module creates a
separate, reproducible research surface by selecting one canonical row per
asset/date using the same provider priorities as the operator-facing ranking
code, then calculating windows in *trading-row* order.  It never writes to the
raw or multi-source tables and it does not produce executable orders.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from trade_system.source_authority import provider_rank_sql

STOCK_FEATURE_TABLE = "qlib_stock_flow_features_v2"
SECTOR_FEATURE_TABLE = "qlib_sector_flow_features_v2"
FEATURE_VERSION = "flow_features_v2_equal_observation_windows"


def _columns(con: duckdb.DuckDBPyConnection, relation: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info('{relation}')").fetchall()}
    except Exception:
        return set()


def _exists(con: duckdb.DuckDBPyConnection, relation: str) -> bool:
    return bool(
        con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?",
            [relation],
        ).fetchone()[0]
    )


def ensure_flow_feature_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create/upgrade the research feature tables without removing history."""

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {STOCK_FEATURE_TABLE} (
            trade_date DATE,
            stock_code VARCHAR,
            provider VARCHAR,
            main_net_1d DOUBLE,
            main_net_3d DOUBLE,
            main_net_5d DOUBLE,
            main_net_10d DOUBLE,
            main_net_20d DOUBLE,
            positive_days_3d INTEGER,
            positive_days_5d INTEGER,
            positive_days_10d INTEGER,
            positive_days_20d INTEGER,
            observed_days_20d INTEGER,
            flow_acceleration_5d DOUBLE,
            main_net_ratio_1d DOUBLE,
            net_total_1d DOUBLE,
            close DOUBLE,
            change_pct DOUBLE,
            quality_status VARCHAR,
            feature_version VARCHAR,
            generated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SECTOR_FEATURE_TABLE} (
            trade_date DATE,
            sector_code VARCHAR,
            sector_name VARCHAR,
            sector_type VARCHAR,
            provider VARCHAR,
            main_net_1d DOUBLE,
            main_net_3d DOUBLE,
            main_net_5d DOUBLE,
            main_net_10d DOUBLE,
            main_net_20d DOUBLE,
            positive_days_3d INTEGER,
            positive_days_5d INTEGER,
            positive_days_10d INTEGER,
            positive_days_20d INTEGER,
            observed_days_20d INTEGER,
            flow_acceleration_5d DOUBLE,
            main_net_ratio_1d DOUBLE,
            change_pct DOUBLE,
            quality_status VARCHAR,
            feature_version VARCHAR,
            generated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )


def _bounds(con: duckdb.DuckDBPyConnection, relation: str, column: str, end_date: str | None) -> tuple[str | None, str | None]:
    if not _exists(con, relation):
        return None, None
    row = con.execute(
        f"SELECT min({column}), max({column}) FROM {relation} "
        + (f"WHERE {column} <= CAST(? AS DATE)" if end_date else ""),
        [end_date] if end_date else [],
    ).fetchone()
    return (str(row[0])[:10] if row[0] is not None else None, str(row[1])[:10] if row[1] is not None else None)


def _stock_query(con: duckdb.DuckDBPyConnection, end_date: str | None) -> tuple[str, list[Any]]:
    cols = _columns(con, "multi_source_stock_flow")
    definition = "flow_definition" if "flow_definition" in cols else "CAST(NULL AS VARCHAR)"
    ratio = "main_net / NULLIF(turnover,0)" if {'turnover_unit','flow_unit'}.issubset(cols) else "CAST(NULL AS DOUBLE)"
    if ratio != "CAST(NULL AS DOUBLE)":
        ratio = "CASE WHEN turnover_unit='CNY' AND flow_unit='CNY' THEN " + ratio + " END"
    net_total = "CAST(net_total AS DOUBLE)" if "net_total" in cols else "CAST(NULL AS DOUBLE)"
    source_end = "AND source_date <= CAST(? AS DATE)" if end_date else ""
    params: list[Any] = [end_date] if end_date else []
    provider_order = provider_rank_sql("stock_flow", "provider")
    query = f"""
    WITH canonical AS (
        SELECT source_date AS trade_date, stock_code, provider, main_net,
               {definition} AS flow_definition, {ratio} AS certified_ratio,
               {net_total} AS net_total, turnover, close, change_pct,
               row_number() OVER (
                   PARTITION BY source_date, stock_code
                   ORDER BY {provider_order} ASC,
                       fetched_at DESC NULLS LAST
               ) AS rn
        FROM multi_source_stock_flow
        WHERE main_net IS NOT NULL AND coalesce(is_stale,FALSE)=FALSE {source_end}
    ), chosen AS (
        SELECT * FROM canonical WHERE rn=1
    ), boundaries AS (
        SELECT *, CASE WHEN provider IS DISTINCT FROM lag(provider) OVER w
            OR flow_definition IS DISTINCT FROM lag(flow_definition) OVER w THEN 1 ELSE 0 END AS boundary
        FROM chosen WINDOW w AS (PARTITION BY stock_code ORDER BY trade_date)
    ), base AS (
        SELECT *, sum(boundary) OVER (PARTITION BY stock_code ORDER BY trade_date) AS source_segment FROM boundaries
    ), features AS (
        SELECT trade_date, stock_code, provider, flow_definition,
               main_net AS main_net_1d,
               SUM(main_net) OVER w3 AS main_net_3d,
               SUM(main_net) OVER w5 AS main_net_5d,
               SUM(main_net) OVER w10 AS main_net_10d,
               SUM(main_net) OVER w20 AS main_net_20d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w3 AS positive_days_3d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w5 AS positive_days_5d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w10 AS positive_days_10d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w20 AS positive_days_20d,
               COUNT(*) OVER w20 AS observed_days_20d,
               CASE WHEN COUNT(*) OVER w10=10 THEN 2*SUM(main_net) OVER w5-SUM(main_net) OVER w10 END AS flow_acceleration_5d,
               certified_ratio AS main_net_ratio_1d,
               net_total, close, change_pct
        FROM base
        WINDOW
            w3 AS (PARTITION BY stock_code,source_segment ORDER BY trade_date ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
            w5 AS (PARTITION BY stock_code,source_segment ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
            w10 AS (PARTITION BY stock_code,source_segment ORDER BY trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
            w20 AS (PARTITION BY stock_code,source_segment ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
    )
    SELECT trade_date, stock_code, provider, main_net_1d, main_net_3d, main_net_5d,
           main_net_10d, main_net_20d, positive_days_3d, positive_days_5d,
           positive_days_10d, positive_days_20d, observed_days_20d,
           flow_acceleration_5d, main_net_ratio_1d, net_total, close, change_pct,
           CASE WHEN flow_definition IS NULL THEN 'source_definition_unverified'
                WHEN observed_days_20d<20 THEN 'insufficient_history'
                WHEN main_net_ratio_1d IS NULL THEN 'ratio_unit_or_denominator_unverified'
                ELSE 'research_candidate_not_certified' END AS quality_status, '{FEATURE_VERSION}' AS feature_version
    FROM features
    """
    return query, params


def _sector_query(con: duckdb.DuckDBPyConnection, end_date: str | None) -> tuple[str, list[Any]]:
    cols = _columns(con, "multi_source_sector_flow")
    definition = "flow_definition" if "flow_definition" in cols else "CAST(NULL AS VARCHAR)"
    sector_type = "coalesce(sector_type,'unknown')" if "sector_type" in cols else "'unknown'"
    source_end = "AND source_date <= CAST(? AS DATE)" if end_date else ""
    params: list[Any] = [end_date] if end_date else []
    query = f"""
    WITH canonical AS (
        SELECT source_date AS trade_date, sector_code, sector_name,
               {sector_type} AS sector_type, provider, main_net, change_pct,
               {definition} AS flow_definition,
               row_number() OVER (
                   PARTITION BY source_date, sector_code, {sector_type}
                   ORDER BY CASE
                       WHEN lower(coalesce(sector_type,''))='ths_concept' THEN 100
                       WHEN lower(coalesce(sector_type,''))='ths_concept_derived' THEN 80
                       WHEN lower(coalesce(sector_type,''))='em_industry' THEN 70
                       WHEN lower(coalesce(sector_type,''))='tushare_dc_sector' THEN 50
                       ELSE 30 END DESC,
                       fetched_at DESC NULLS LAST
               ) AS rn
        FROM multi_source_sector_flow
        WHERE main_net IS NOT NULL AND coalesce(is_stale,FALSE)=FALSE {source_end}
    ), chosen AS (
        SELECT * FROM canonical WHERE rn=1
    ), boundaries AS (
        SELECT *, CASE WHEN provider IS DISTINCT FROM lag(provider) OVER w
            OR flow_definition IS DISTINCT FROM lag(flow_definition) OVER w THEN 1 ELSE 0 END AS boundary
        FROM chosen WINDOW w AS (PARTITION BY sector_code,sector_type ORDER BY trade_date)
    ), base AS (
        SELECT *, sum(boundary) OVER (PARTITION BY sector_code,sector_type ORDER BY trade_date) AS source_segment FROM boundaries
    ), features AS (
        SELECT trade_date, sector_code, sector_name, sector_type, provider,
               main_net AS main_net_1d,
               SUM(main_net) OVER w3 AS main_net_3d,
               SUM(main_net) OVER w5 AS main_net_5d,
               SUM(main_net) OVER w10 AS main_net_10d,
               SUM(main_net) OVER w20 AS main_net_20d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w3 AS positive_days_3d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w5 AS positive_days_5d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w10 AS positive_days_10d,
               SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) OVER w20 AS positive_days_20d,
               COUNT(*) OVER w20 AS observed_days_20d,
               CASE WHEN COUNT(*) OVER w10=10 THEN 2*SUM(main_net) OVER w5-SUM(main_net) OVER w10 END AS flow_acceleration_5d,
               change_pct
        FROM base
        WINDOW
            w3 AS (PARTITION BY sector_code, sector_type,source_segment ORDER BY trade_date ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
            w5 AS (PARTITION BY sector_code, sector_type,source_segment ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
            w10 AS (PARTITION BY sector_code, sector_type,source_segment ORDER BY trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
            w20 AS (PARTITION BY sector_code, sector_type,source_segment ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
    )
    SELECT trade_date, sector_code, sector_name, sector_type, provider,
           main_net_1d, main_net_3d, main_net_5d, main_net_10d, main_net_20d,
           positive_days_3d, positive_days_5d, positive_days_10d, positive_days_20d,
           observed_days_20d, flow_acceleration_5d, CAST(NULL AS DOUBLE) AS main_net_ratio_1d,
           change_pct, 'sector_source_definition_unverified' AS quality_status, '{FEATURE_VERSION}' AS feature_version
    FROM features
    """
    return query, params


def build_flow_features(
    db_path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Rebuild the requested feature range idempotently.

    The source scan includes all rows up to ``end_date`` so a requested range
    retains the preceding 20 trading observations needed for rolling windows.
    """

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        ensure_flow_feature_tables(con)
        stock_min, stock_max = _bounds(con, "multi_source_stock_flow", "source_date", end_date)
        sector_min, sector_max = _bounds(con, "multi_source_sector_flow", "source_date", end_date)
        target_start = start_date or stock_min or sector_min
        target_end = end_date or stock_max or sector_max
        if not target_start or not target_end:
            return {"stock_rows": 0, "sector_rows": 0, "start_date": target_start, "end_date": target_end, "feature_version": FEATURE_VERSION}

        con.execute("BEGIN TRANSACTION")
        try:
            if stock_min:
                stock_sql, stock_params = _stock_query(con, target_end)
                con.execute(f"DELETE FROM {STOCK_FEATURE_TABLE} WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)", [target_start, target_end])
                con.execute(
                    f"""
                    INSERT INTO {STOCK_FEATURE_TABLE} (
                        trade_date, stock_code, provider, main_net_1d, main_net_3d,
                        main_net_5d, main_net_10d, main_net_20d, positive_days_3d,
                        positive_days_5d, positive_days_10d, positive_days_20d,
                        observed_days_20d, flow_acceleration_5d, main_net_ratio_1d,
                        net_total_1d, close, change_pct, quality_status, feature_version
                    )
                    SELECT * FROM ({stock_sql}) q
                    WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                    """,
                    [*stock_params, target_start, target_end],
                )
            if sector_min:
                sector_sql, sector_params = _sector_query(con, target_end)
                con.execute(f"DELETE FROM {SECTOR_FEATURE_TABLE} WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)", [target_start, target_end])
                con.execute(
                    f"""
                    INSERT INTO {SECTOR_FEATURE_TABLE} (
                        trade_date, sector_code, sector_name, sector_type, provider,
                        main_net_1d, main_net_3d, main_net_5d, main_net_10d,
                        main_net_20d, positive_days_3d, positive_days_5d,
                        positive_days_10d, positive_days_20d, observed_days_20d,
                        flow_acceleration_5d, main_net_ratio_1d, change_pct,
                        quality_status, feature_version
                    )
                    SELECT * FROM ({sector_sql}) q
                    WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                    """,
                    [*sector_params, target_start, target_end],
                )

            stock_rows = int(con.execute(f"SELECT count(*) FROM {STOCK_FEATURE_TABLE} WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)", [target_start, target_end]).fetchone()[0])
            sector_rows = int(con.execute(f"SELECT count(*) FROM {SECTOR_FEATURE_TABLE} WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)", [target_start, target_end]).fetchone()[0])
            result = {
                "stock_rows": stock_rows,
                "sector_rows": sector_rows,
                "start_date": target_start,
                "end_date": target_end,
                "feature_version": FEATURE_VERSION,
                "generated_at": date.today().isoformat(),
            }
            con.commit()
            return result
        except Exception:
            con.rollback()
            raise
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()
