"""DuckDB persistence for strategy definitions and scan results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from trade_system.strategy.definition import StrategyDefinition


SCAN_COLUMNS = [
    "trade_date",
    "strategy_id",
    "symbol",
    "stock_name",
    "stage",
    "score",
    "evidence_json",
    "selected_reason",
    "risk_points",
    "invalid_conditions",
]


def install_strategy_tables(db_path: str | Path) -> None:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_definition (
                strategy_id VARCHAR PRIMARY KEY,
                name VARCHAR,
                stage VARCHAR,
                version VARCHAR,
                enabled BOOLEAN,
                config_json VARCHAR,
                entry_rules_json VARCHAR,
                exit_rules_json VARCHAR,
                risk_rules_json VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_scan_result (
                trade_date VARCHAR,
                strategy_id VARCHAR,
                symbol VARCHAR,
                stock_name VARCHAR,
                stage VARCHAR,
                score DOUBLE,
                evidence_json VARCHAR,
                selected_reason VARCHAR,
                risk_points VARCHAR,
                invalid_conditions VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_backtest_result (
                strategy_id VARCHAR,
                stage VARCHAR,
                sample_start VARCHAR,
                sample_end VARCHAR,
                sample_count INTEGER,
                win_rate DOUBLE,
                avg_return DOUBLE,
                max_drawdown DOUBLE,
                profit_factor DOUBLE,
                config_hash VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE OR REPLACE VIEW v_stage_strategy_candidates AS
            SELECT
                trade_date,
                stage,
                strategy_id,
                symbol,
                stock_name,
                score,
                selected_reason,
                risk_points,
                invalid_conditions
            FROM strategy_scan_result
            """
        )
    finally:
        con.close()


def persist_strategy_scan_results(db_path: str | Path, rows: list[dict[str, Any]]) -> int:
    install_strategy_tables(db_path)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        # A scan is a complete materialized snapshot of the currently
        # actionable operator candidates.  Keeping rows that disappeared from
        # the source view resurrects legacy/non-actionable signals in backtests.
        con.execute("DELETE FROM strategy_scan_result")
        for row in rows:
            con.execute(
                """
                DELETE FROM strategy_scan_result
                WHERE trade_date = ? AND strategy_id = ? AND symbol = ? AND stage = ?
                """,
                [row["trade_date"], row["strategy_id"], row["symbol"], row["stage"]],
            )
            values = [row.get(column) for column in SCAN_COLUMNS]
            placeholders = ", ".join(["?"] * len(SCAN_COLUMNS))
            column_sql = ", ".join(f'"{column}"' for column in SCAN_COLUMNS)
            con.execute(f"INSERT INTO strategy_scan_result ({column_sql}) VALUES ({placeholders})", values)
        return len(rows)
    finally:
        con.close()


def persist_strategy_definitions(db_path: str | Path, strategies: list[StrategyDefinition]) -> int:
    install_strategy_tables(db_path)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        for strategy in strategies:
            con.execute("DELETE FROM strategy_definition WHERE strategy_id = ?", [strategy.strategy_id])
            con.execute(
                """
                INSERT INTO strategy_definition (
                    strategy_id, name, stage, version, enabled, config_json,
                    entry_rules_json, exit_rules_json, risk_rules_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    strategy.strategy_id,
                    strategy.name,
                    strategy.stage,
                    "1",
                    True,
                    json.dumps({"min_score": strategy.min_score, "score_field": strategy.score_field}, ensure_ascii=False),
                    json.dumps(strategy.entry_rules, ensure_ascii=False),
                    json.dumps(strategy.invalid_conditions, ensure_ascii=False),
                    json.dumps(strategy.risk_rules, ensure_ascii=False),
                ],
            )
        return len(strategies)
    finally:
        con.close()


def persist_strategy_backtest_summary(db_path: str | Path, rows: list[dict[str, Any]]) -> int:
    install_strategy_tables(db_path)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        for row in rows:
            con.execute(
                """
                DELETE FROM strategy_backtest_result
                WHERE strategy_id = ? AND stage = ? AND config_hash = ?
                """,
                [row["strategy_id"], row["stage"], row.get("config_hash", "")],
            )
            con.execute(
                """
                INSERT INTO strategy_backtest_result (
                    strategy_id, stage, sample_start, sample_end, sample_count,
                    win_rate, avg_return, max_drawdown, profit_factor, config_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["strategy_id"],
                    row["stage"],
                    row.get("sample_start", ""),
                    row.get("sample_end", ""),
                    int(row.get("sample_count", 0)),
                    row.get("win_rate"),
                    row.get("avg_return"),
                    row.get("max_drawdown"),
                    row.get("profit_factor"),
                    row.get("config_hash", ""),
                ],
            )
        return len(rows)
    finally:
        con.close()
