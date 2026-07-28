"""Critical business-key repair and enforcement for production tables."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.quality import dedupe_table, table_columns, table_exists


KTYPE_TABLES = ("kline", "index_kline", "advanced_kline_today", "advanced_gujia_kline")

DEDUPE_SPECS = (
    ("kline", ("date", "stock_code", "ktype"), "fetched_at"),
    ("index_kline", ("date", "index_code", "ktype"), "fetched_at"),
    ("advanced_kline_today", ("date", "stock_code", "ktype"), "fetched_at"),
    ("advanced_gujia_kline", ("date", "stock_code", "ktype"), "fetched_at"),
    ("market_regime_snapshot", ("trade_date",), "generated_at"),
    ("sector_rotation_score", ("trade_date", "sector_code"), "generated_at"),
    ("stock_candidate_score", ("trade_date", "stock_code"), "generated_at"),
    ("stock_candidate_stage_signal", ("trade_date", "stage", "stock_code"), "generated_at"),
    ("alert_events", ("trade_date", "severity", "category", "message"), "generated_at"),
    ("watchlist", ("trade_date", "stock_code"), "created_at"),
    ("trade_plan", ("trade_date", "stock_code"), "created_at"),
    ("risk_snapshot", ("trade_date",), "created_at"),
    ("portfolio_snapshot", ("trade_date", "snapshot_time", "stock_code"), "created_at"),
)

UNIQUE_INDEX_SPECS = (
    ("uq_kline_business", "kline", ("date", "stock_code", "ktype")),
    ("uq_index_kline_business", "index_kline", ("date", "index_code", "ktype")),
    ("uq_market_regime_date", "market_regime_snapshot", ("trade_date",)),
    ("uq_sector_rotation_date_code", "sector_rotation_score", ("trade_date", "sector_code")),
    ("uq_candidate_date_code", "stock_candidate_score", ("trade_date", "stock_code")),
    (
        "uq_stage_signal_date_stage_code",
        "stock_candidate_stage_signal",
        ("trade_date", "stage", "stock_code"),
    ),
    ("uq_watchlist_date_code", "watchlist", ("trade_date", "stock_code")),
    ("uq_trade_plan_date_code", "trade_plan", ("trade_date", "stock_code")),
    ("uq_risk_snapshot_date", "risk_snapshot", ("trade_date",)),
    (
        "uq_portfolio_snapshot_business",
        "portfolio_snapshot",
        ("trade_date", "snapshot_time", "stock_code"),
    ),
)


def normalize_kline_periods(db_path: str | Path) -> dict[str, int]:
    con = duckdb.connect(str(db_path))
    changed: dict[str, int] = {}
    try:
        for table in KTYPE_TABLES:
            if not table_exists(con, table) or "ktype" not in table_columns(con, table):
                continue
            before = int(
                con.execute(
                    f"SELECT count(*) FROM \"{table}\" "
                    "WHERE ktype IS NOT NULL AND ktype != upper(trim(ktype))"
                ).fetchone()[0]
            )
            con.execute(
                f"UPDATE \"{table}\" SET ktype = upper(trim(ktype)) "
                "WHERE ktype IS NOT NULL AND ktype != upper(trim(ktype))"
            )
            changed[table] = before
    finally:
        con.close()
    return changed


def ensure_unique_indexes(db_path: str | Path) -> list[str]:
    con = duckdb.connect(str(db_path))
    created: list[str] = []
    try:
        for index_name, table, columns in UNIQUE_INDEX_SPECS:
            if not table_exists(con, table):
                continue
            existing = set(table_columns(con, table))
            if not set(columns) <= existing:
                continue
            column_sql = ", ".join(f'"{column}"' for column in columns)
            con.execute(
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{index_name}" ON "{table}" ({column_sql})'
            )
            created.append(index_name)
    finally:
        con.close()
    return created


def repair_critical_integrity(db_path: str | Path, dry_run: bool = False) -> dict:
    normalized = {} if dry_run else normalize_kline_periods(db_path)
    repairs = []
    for table, keys, order_column in DEDUPE_SPECS:
        result = dedupe_table(
            db_path,
            table,
            keys,
            order_column=order_column,
            dry_run=dry_run,
        )
        repairs.append(result)

    plan_rows_blocked = 0
    if not dry_run:
        from trade_system.stage_signals import ensure_stage_signal_schema

        con = duckdb.connect(str(db_path))
        try:
            ensure_stage_signal_schema(con)
            if table_exists(con, "trade_plan"):
                plan_rows_blocked = int(
                    con.execute(
                        "SELECT count(*) FROM trade_plan "
                        "WHERE coalesce(max_position_pct, 0) <= 0 "
                        "AND coalesce(status, '') != 'blocked_data_quality'"
                    ).fetchone()[0]
                )
                con.execute(
                    "UPDATE trade_plan SET status='blocked_data_quality' "
                    "WHERE coalesce(max_position_pct, 0) <= 0"
                )
                if table_exists(con, "watchlist"):
                    con.execute(
                        "UPDATE watchlist SET status='blocked_data_quality' "
                        "WHERE trade_date IN ("
                        "SELECT trade_date FROM risk_snapshot "
                        "WHERE coalesce(max_single_position_pct, 0) <= 0)"
                    )
        finally:
            con.close()
        indexes = ensure_unique_indexes(db_path)
    else:
        indexes = []

    return {
        "dry_run": dry_run,
        "normalized_ktype_rows": normalized,
        "repairs": repairs,
        "plan_rows_blocked": plan_rows_blocked,
        "unique_indexes": indexes,
    }
