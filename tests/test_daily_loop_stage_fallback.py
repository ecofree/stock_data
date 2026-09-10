from __future__ import annotations

import duckdb
import json

from trade_system.daily_loop import run_daily_operator_loop
from trade_system.risk import init_trading_tables
from trade_system.stage_signals import ensure_stage_signal_schema


def test_daily_operator_loop_uses_actionable_stage_pool_when_scores_absent(tmp_path):
    db = tmp_path / "daily-loop-stage.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE stock_candidate_score (trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, source VARCHAR, sector_code VARCHAR, evidence_json VARCHAR, is_actionable BOOLEAN)"
    )
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal (trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, decision VARCHAR, evidence_json VARCHAR, is_actionable BOOLEAN)"
    )
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-16','intraday_strength','000001','Ping An',75,'follow','{}',true)"
    )
    con.execute(
        "CREATE TABLE market_regime_snapshot (trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, evidence_json VARCHAR, generated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_regime_snapshot VALUES ('2026-07-16','normal',70,30,'{}',current_timestamp)"
    )
    con.close()

    result = run_daily_operator_loop(db, "2026-07-16", 20, stage="intraday")

    assert result["watchlist"] == 1
    assert result["trade_plan"] == 1
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT count(*) FROM trade_plan WHERE trade_date='2026-07-16'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_intraday_risk_loop_requires_verified_account_even_for_actionable_candidate(
    tmp_path, monkeypatch
):
    db = tmp_path / "daily-loop-risk.duckdb"
    init_trading_tables(db)
    con = duckdb.connect(str(db))
    ensure_stage_signal_schema(con)
    con.execute(
        "CREATE TABLE stock_candidate_score("
        "trade_date VARCHAR,stock_code VARCHAR,stock_name VARCHAR,score DOUBLE,"
        "source VARCHAR,sector_code VARCHAR,evidence_json VARCHAR,is_actionable BOOLEAN)"
    )
    con.execute(
        "CREATE TABLE market_regime_snapshot("
        "trade_date VARCHAR,regime VARCHAR,regime_score DOUBLE,"
        "suggested_position_pct INTEGER,evidence_json VARCHAR,generated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_regime_snapshot VALUES "
        "('2026-07-31','normal',75,30,'{}',current_timestamp)"
    )
    con.execute(
        "INSERT INTO stock_candidate_score VALUES "
        "('2026-07-31','000001','Alpha',88,'stage','801001','{}',true)"
    )
    con.execute(
        """
        INSERT INTO stock_candidate_stage_signal(
            trade_date,stage,stock_code,stock_name,score,decision,evidence_json,
            is_actionable,data_complete,signal_triggered,tradable,
            risk_approved,is_executable
        )
        VALUES (
            '2026-07-31','intraday_strength','000001','Alpha',88,'follow','{}',
            true,true,true,true,true,true
        )
        """
    )
    con.execute(
        """
        INSERT INTO stock_candidate_stage_signal(
            trade_date,stage,stock_code,stock_name,score,decision,evidence_json,
            is_actionable,data_complete,signal_triggered,tradable,
            risk_approved,is_executable
        )
        VALUES (
            '2026-07-31','intraday_strength','000002','WatchOnly',99,'watch','{}',
            true,true,false,true,true,true
        )
        """
    )
    con.close()
    monkeypatch.setattr(
        "trade_system.daily_loop.assess_trade_date_readiness",
        lambda *args, **kwargs: {"ready": True, "analytics_ready": True},
    )

    result = run_daily_operator_loop(
        db, "2026-07-31", 20, stage="intraday"
    )

    con = duckdb.connect(str(db), read_only=True)
    try:
        promoted = con.execute(
            "SELECT stock_code,risk_approved,is_executable "
            "FROM stock_candidate_stage_signal "
            "WHERE stage='intraday_strength' ORDER BY stock_code"
        ).fetchall()
        total, drawdown, evidence = con.execute(
            "SELECT total_position_pct,current_drawdown_pct,evidence_json FROM risk_snapshot"
        ).fetchone()
        plan = con.execute("SELECT max_position_pct,status FROM trade_plan").fetchone()
    finally:
        con.close()
    assert result["trade_plan"] == 1
    assert promoted == [
        ("000001", False, False),
        ("000002", False, False),
    ]
    # Actionable market data is not a complete account. Old approvals must
    # be revoked, not implicitly interpreted as a verified zero position.
    assert total is None and drawdown is None
    assert json.loads(evidence)["execution_blockers"] == ["account_snapshot_unverified"]
    assert plan == (0.0, "review_required")
