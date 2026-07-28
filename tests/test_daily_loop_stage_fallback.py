from __future__ import annotations

import duckdb

from trade_system.daily_loop import run_daily_operator_loop


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

    result = run_daily_operator_loop(db, "2026-07-16", 20)

    assert result["watchlist"] == 1
    assert result["trade_plan"] == 1
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM trade_plan WHERE trade_date='2026-07-16'").fetchone()[0] == 1
    con.close()
