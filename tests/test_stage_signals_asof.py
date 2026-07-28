import json

import duckdb

from trade_system.normalize import build_normalized_views
from trade_system.signals import generate_signals
from trade_system.stage_signals import ensure_stage_signal_schema, generate_stage_signals


def _build_context(db_path, trade_date="2026-07-08"):
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, "
        "fall_count INTEGER, consecutive_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES (?,50,6,3000,1000,5,? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE sector_strength("
        "date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, "
        "fengban_rate DOUBLE, dieting INTEGER, up_count INTEGER, down_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_strength VALUES (?,'801001',80,8,70,0,40,5,? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE sector_ranking("
        "date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_ranking VALUES (?,'801001','test sector',20,? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE sector_capital("
        "date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, "
        "big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_capital VALUES (?,'801001',500000000,100000000,80000000,50000000,-10000000,? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE sector_stocks("
        "date DATE, sector_code VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "change_pct DOUBLE, turnover BIGINT, market_cap BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_stocks VALUES (?,'801001','000001','test stock',5.5,1000000,1000000000,? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE l2_realtime_all_boards("
        "date DATE, board_level INTEGER, stock_code VARCHAR, stock_name VARCHAR, "
        "limit_up_time VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_realtime_all_boards VALUES (?,2,'000001','test stock','09:35',? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO kline VALUES (?,'000001',9.5,10.1,9.4,10.0,100000,1000000,5.5,'D',? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE l2_stock_intraday("
        "date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE, avg_price DOUBLE, "
        "volume BIGINT, turnover BIGINT, main_fund_net BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_stock_intraday VALUES (?,'000001','10:30',10,9.8,50000,500000,20000000,? || ' 10:30:00')",
        [trade_date, trade_date],
    )
    con.close()
    build_normalized_views(str(db_path))
    generate_signals(str(db_path), trade_date)


def test_premarket_signal_uses_previous_trade_date_context(tmp_path):
    db_path = tmp_path / "premarket.duckdb"
    _build_context(db_path)

    result = generate_stage_signals(
        db_path,
        "2026-07-09",
        "premarket_pool",
        as_of_time="2026-07-09T08:30:00",
        run_id="test",
    )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(
            "SELECT source_trade_date, as_of_time, is_actionable, feature_version, evidence_json "
            "FROM stock_candidate_stage_signal"
        ).fetchone()
    finally:
        con.close()

    evidence = json.loads(row[4])
    assert result["actionable"] == 1
    assert row[:4] == ("2026-07-08", row[1], True, "stage_v2_asof")
    assert str(row[1]) == "2026-07-09 08:30:00"
    assert evidence["source_trade_date"] == "2026-07-08"
    assert evidence["input_cutoff_enforced"] is True


def test_stage_signal_outside_window_is_non_actionable(tmp_path):
    db_path = tmp_path / "blocked.duckdb"
    _build_context(db_path)

    result = generate_stage_signals(
        db_path,
        "2026-07-09",
        "premarket_pool",
        as_of_time="2026-07-09T10:00:00",
    )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        decision, actionable = con.execute(
            "SELECT decision, is_actionable FROM stock_candidate_stage_signal"
        ).fetchone()
    finally:
        con.close()

    assert result["within_stage_window"] is False
    assert result["actionable"] == 0
    assert (decision, actionable) == ("blocked_data_quality", False)


def test_close_signal_is_actionable_only_after_complete_same_date_context(tmp_path):
    db_path = tmp_path / "close.duckdb"
    _build_context(db_path)

    result = generate_stage_signals(
        db_path,
        "2026-07-08",
        "close_decision",
        as_of_time="2026-07-08T15:10:00",
    )

    assert result["readiness"]["ready"] is True
    assert result["actionable"] == 1


def test_close_signal_blocks_stock_without_its_own_close_price(tmp_path):
    db_path = tmp_path / "close_missing_stock_price.duckdb"
    _build_context(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "INSERT INTO stock_candidate_score "
            "(trade_date, stock_code, stock_name, score, source, sector_code, evidence_json) "
            "VALUES ('2026-07-08','000002','no price stock',88,'test','801001','{}')"
        )
    finally:
        con.close()

    result = generate_stage_signals(
        db_path,
        "2026-07-08",
        "close_decision",
        as_of_time="2026-07-08T15:10:00",
    )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT stock_code, is_actionable, evidence_json "
            "FROM stock_candidate_stage_signal ORDER BY stock_code"
        ).fetchall()
    finally:
        con.close()

    assert result["inserted"] == 2
    assert result["actionable"] == 1
    assert rows[0][0:2] == ("000001", True)
    assert rows[1][0:2] == ("000002", False)
    assert json.loads(rows[1][2])["row_block_reason"] == "missing_stock_close_price"


def test_intraday_signal_excludes_source_times_after_as_of(tmp_path):
    db_path = tmp_path / "intraday_cutoff.duckdb"
    _build_context(db_path, "2026-07-09")
    con = duckdb.connect(str(db_path))
    try:
        ensure_stage_signal_schema(con)
        con.execute(
            "UPDATE sector_capital SET fetched_at='2026-07-09 10:30:00' "
            "WHERE date='2026-07-09'"
        )
        con.execute(
            "INSERT INTO l2_stock_intraday VALUES "
            "('2026-07-09','000001','14:00',11,10,50000,500000,999000000," 
            "'2026-07-09 10:35:00')"
        )
        con.execute(
            "INSERT OR REPLACE INTO stock_candidate_stage_signal "
            "(trade_date,stage,stock_code,stock_name,score,decision,evidence_json," 
            "source_trade_date,as_of_time,run_id,is_actionable,feature_version) "
            "VALUES ('2026-07-09','auction_confirmation','000001','test stock',80," 
            "'confirm','{}','2026-07-09','2026-07-09 09:25:00','test',true,'stage_v2_asof')"
        )
    finally:
        con.close()

    result = generate_stage_signals(
        db_path,
        "2026-07-09",
        "intraday_strength",
        as_of_time="2026-07-09T10:40:00",
    )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        evidence_json = con.execute(
            "SELECT evidence_json FROM stock_candidate_stage_signal "
            "WHERE stage='intraday_strength'"
        ).fetchone()[0]
    finally:
        con.close()
    intraday = json.loads(evidence_json)["stage_evidence"]

    assert result["actionable"] == 1
    assert intraday["l2_stock_intraday_rows"] == 1
    assert intraday["l2_stock_intraday_latest_source_time"] == "10:30"
    assert intraday["active_fund_net"] == 20_000_000
