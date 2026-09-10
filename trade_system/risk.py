"""Operator-facing trading and risk tables."""

from __future__ import annotations

from pathlib import Path


# Canonical definition shared with trade_system.operator_outcomes (which
# already imports this module).  Both ensure-paths execute the same
# CREATE OR REPLACE VIEW so execution order never changes the result.
OPERATOR_PLAN_OUTCOME_VIEW_SQL = """
CREATE OR REPLACE VIEW v_operator_plan_outcome AS
SELECT
    coalesce(p.trade_date, o.trade_date) AS trade_date,
    coalesce(p.stock_code, o.stock_code) AS stock_code,
    coalesce(p.stock_name, o.stock_name) AS stock_name,
    p.setup_type,
    p.max_position_pct AS planned_position_pct,
    p.status AS plan_status,
    o.execution_status,
    o.position_pct AS actual_position_pct,
    o.gross_return_pct,
    o.net_return_pct,
    o.outcome_tag,
    o.mistake_tag,
    o.review_note,
    o.imported_from,
    o.created_at AS outcome_created_at
FROM trade_plan p
FULL OUTER JOIN operator_trade_outcome o
    ON p.trade_date = o.trade_date AND p.stock_code = o.stock_code
"""


def init_trading_tables(db_path: str | Path) -> list[str]:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                trade_date VARCHAR,
                stock_code VARCHAR,
                stock_name VARCHAR,
                sector_code VARCHAR,
                thesis VARCHAR,
                invalidation VARCHAR,
                priority INTEGER,
                status VARCHAR DEFAULT 'active',
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_plan (
                trade_date VARCHAR,
                stock_code VARCHAR,
                stock_name VARCHAR,
                setup_type VARCHAR,
                entry_condition VARCHAR,
                stop_condition VARCHAR,
                target_condition VARCHAR,
                max_position_pct DOUBLE,
                status VARCHAR DEFAULT 'planned',
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_snapshot (
                trade_date VARCHAR,
                snapshot_time VARCHAR,
                stock_code VARCHAR,
                stock_name VARCHAR,
                position_pct DOUBLE,
                cost_price DOUBLE,
                last_price DOUBLE,
                pnl_pct DOUBLE,
                sector_code VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_journal (
                trade_date VARCHAR,
                stock_code VARCHAR,
                stock_name VARCHAR,
                action VARCHAR,
                action_time VARCHAR,
                price DOUBLE,
                position_pct DOUBLE,
                reason VARCHAR,
                mistake_tag VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_trade_outcome (
                trade_date VARCHAR,
                stock_code VARCHAR,
                stock_name VARCHAR,
                execution_status VARCHAR,
                entry_time VARCHAR,
                exit_time VARCHAR,
                entry_price DOUBLE,
                exit_price DOUBLE,
                position_pct DOUBLE,
                gross_return_pct DOUBLE,
                net_return_pct DOUBLE,
                outcome_tag VARCHAR,
                mistake_tag VARCHAR,
                review_note VARCHAR,
                imported_from VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_snapshot (
                trade_date VARCHAR,
                total_position_pct DOUBLE,
                max_single_position_pct DOUBLE,
                max_sector_position_pct DOUBLE,
                daily_loss_limit_pct DOUBLE,
                current_drawdown_pct DOUBLE,
                risk_state VARCHAR,
                evidence_json VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(OPERATOR_PLAN_OUTCOME_VIEW_SQL)
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_date_code "
            "ON watchlist(trade_date, stock_code)"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_plan_date_code "
            "ON trade_plan(trade_date, stock_code)"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_snapshot_date "
            "ON risk_snapshot(trade_date)"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_snapshot_business "
            "ON portfolio_snapshot(trade_date, snapshot_time, stock_code)"
        )
        return [
            "watchlist",
            "trade_plan",
            "portfolio_snapshot",
            "trade_journal",
            "operator_trade_outcome",
            "risk_snapshot",
        ]
    finally:
        con.close()
