import json

import duckdb

from trade_system.daily_loop import run_daily_operator_loop
from trade_system.risk import init_trading_tables


def test_daily_operator_loop_populates_manual_workflow_and_caps_weak_market_position(tmp_path):
    db_path = tmp_path / "daily_loop.duckdb"
    init_trading_tables(db_path)
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE market_regime_snapshot("
        "trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, "
        "evidence_json VARCHAR, generated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_regime_snapshot VALUES "
        "('2026-07-06','weak',20,5,'{\"acute_drop_risk_score\": 72}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_rotation_score("
        "trade_date VARCHAR, sector_code VARCHAR, sector_name VARCHAR, score DOUBLE, "
        "strength_value DOUBLE, limit_up_count INTEGER, seal_rate DOUBLE, evidence_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO sector_rotation_score VALUES "
        "('2026-07-06','801001','Theme',72,80,6,60,'{\"why\":\"mainline\"}')"
    )
    con.execute(
        "CREATE TABLE stock_candidate_score("
        "trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, "
        "source VARCHAR, sector_code VARCHAR, evidence_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO stock_candidate_score VALUES "
        "('2026-07-06','000001','Alpha',78,'limit_pool','801001',"
        "'{\"entry_reason\":\"leader\",\"risk_points\":[\"weak market\"],\"invalidation\":\"break board\"}')"
    )
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)"
    )
    for stage, decision in [
        ("premarket_pool", "pool"),
        ("auction_confirmation", "watch"),
        ("intraday_strength", "watch"),
        ("close_decision", "reduce"),
    ]:
        con.execute(
            "INSERT INTO stock_candidate_stage_signal VALUES "
            "('2026-07-06', ?, '000001', 'Alpha', 70, ?, '{}', '2026-07-06 15:00:00')",
            [stage, decision],
        )
    con.close()

    result = run_daily_operator_loop(db_path, "2026-07-06")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {
            table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ["watchlist", "trade_plan", "risk_snapshot", "portfolio_snapshot", "trade_journal"]
        }
        max_position = con.execute("SELECT max(max_position_pct) FROM trade_plan").fetchone()[0]
        risk_state, evidence_json = con.execute("SELECT risk_state, evidence_json FROM risk_snapshot").fetchone()
    finally:
        con.close()

    assert result["watchlist"] == 1
    assert counts == {
        "watchlist": 1,
        "trade_plan": 1,
        "risk_snapshot": 1,
        "portfolio_snapshot": 1,
        "trade_journal": 1,
    }
    assert max_position <= 5
    assert risk_state == "defensive"
    assert json.loads(evidence_json)["suggested_position_pct"] == 5


def test_daily_operator_loop_preserves_imported_operator_outcome_journal(tmp_path):
    db_path = tmp_path / "daily_loop_preserve.duckdb"
    init_trading_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE market_regime_snapshot("
            "trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, "
            "evidence_json VARCHAR, generated_at TIMESTAMP)"
        )
        con.execute("INSERT INTO market_regime_snapshot VALUES ('2026-07-06','weak',20,5,'{}',current_timestamp)")
        con.execute(
            "CREATE TABLE stock_candidate_score("
            "trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, "
            "source VARCHAR, sector_code VARCHAR, evidence_json VARCHAR)"
        )
        con.execute(
            "CREATE TABLE stock_candidate_stage_signal("
            "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
            "score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)"
        )
        con.execute(
            """
            INSERT INTO trade_journal (
                trade_date, stock_code, stock_name, action, action_time, price,
                position_pct, reason, mistake_tag
            )
            VALUES ('2026-07-06','000001','Alpha','operator_outcome','15:00',10.8,5.0,'manual review','none')
            """
        )
    finally:
        con.close()

    run_daily_operator_loop(db_path, "2026-07-06")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        count = con.execute(
            "SELECT count(*) FROM trade_journal WHERE trade_date = '2026-07-06' AND action = 'operator_outcome'"
        ).fetchone()[0]
    finally:
        con.close()

    assert count == 1


def test_daily_operator_loop_marks_zero_position_plan_as_data_blocked(tmp_path):
    db_path = tmp_path / "daily_loop_blocked.duckdb"
    init_trading_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE market_regime_snapshot("
            "trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, "
            "evidence_json VARCHAR, generated_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO market_regime_snapshot VALUES "
            "('2026-07-09','数据缺失',0,0,'{}',current_timestamp)"
        )
        con.execute(
            "CREATE TABLE stock_candidate_score("
            "trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, "
            "source VARCHAR, sector_code VARCHAR, evidence_json VARCHAR)"
        )
        con.execute(
            "INSERT INTO stock_candidate_score VALUES "
            "('2026-07-09','000001','Alpha',99,'test','801001','{}')"
        )
        con.execute(
            "CREATE TABLE stock_candidate_stage_signal("
            "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
            "score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)"
        )
    finally:
        con.close()

    run_daily_operator_loop(db_path, "2026-07-09")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        plan = con.execute("SELECT max_position_pct, status FROM trade_plan").fetchone()
        watchlist_status = con.execute("SELECT status FROM watchlist").fetchone()[0]
    finally:
        con.close()

    assert plan == (0.0, "blocked_data_quality")
    assert watchlist_status == "blocked_data_quality"


def test_daily_operator_loop_is_idempotent_for_repeated_stage_refresh(tmp_path):
    db_path = tmp_path / "daily_loop_repeat.duckdb"
    init_trading_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE market_regime_snapshot("
            "trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, "
            "evidence_json VARCHAR, generated_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO market_regime_snapshot VALUES "
            "('2026-07-10','normal',70,30,'{}',current_timestamp)"
        )
        con.execute(
            "CREATE TABLE stock_candidate_score("
            "trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, "
            "source VARCHAR, sector_code VARCHAR, evidence_json VARCHAR)"
        )
        con.execute(
            "INSERT INTO stock_candidate_score VALUES "
            "('2026-07-10','000001','Alpha',80,'test','801001','{}')"
        )
        con.execute(
            "CREATE TABLE stock_candidate_stage_signal("
            "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
            "score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)"
        )
    finally:
        con.close()

    run_daily_operator_loop(db_path, "2026-07-10")
    run_daily_operator_loop(db_path, "2026-07-10")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {
            table: con.execute(
                f"SELECT count(*) FROM {table} WHERE trade_date='2026-07-10'"
            ).fetchone()[0]
            for table in ("watchlist", "trade_plan", "risk_snapshot", "portfolio_snapshot")
        }
    finally:
        con.close()
    assert counts == {
        "watchlist": 1,
        "trade_plan": 1,
        "risk_snapshot": 1,
        "portfolio_snapshot": 1,
    }
