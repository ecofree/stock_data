import json

import duckdb

from trade_system.normalize import build_normalized_views
from trade_system.signals import generate_signals


def _calibration_db(path, *, weak_market: bool = False):
    con = duckdb.connect(str(path))
    if weak_market:
        daily = ("2026-07-06", 12, 65, 900, 3600, 1)
        rise_fall = ("2026-07-06", 12, 65, 45, 80, 76.0)
    else:
        daily = ("2026-07-06", 55, 8, 3000, 1200, 5)
        rise_fall = ("2026-07-06", 55, 8, 10, 12, 20.0)
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, "
        "fall_count INTEGER, consecutive_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES (?, ?, ?, ?, ?, ?, '2026-07-06 15:00:00')",
        daily,
    )
    con.execute(
        "CREATE TABLE market_rise_fall("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, broken_limit_up_count INTEGER, "
        "blown_limit_up_count INTEGER, blown_limit_up_rate DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_rise_fall VALUES (?, ?, ?, ?, ?, ?, '2026-07-06 15:00:00')",
        rise_fall,
    )
    con.execute(
        "CREATE TABLE sector_strength("
        "date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, "
        "fengban_rate DOUBLE, dieting INTEGER, up_count INTEGER, down_count INTEGER, fetched_at TIMESTAMP)"
    )
    for code, limit_up, inflow in [
        ("801001", 18, 900000000),
        ("801002", 16, 700000000),
        ("801003", 14, 500000000),
    ]:
        con.execute(
            "INSERT INTO sector_strength VALUES ('2026-07-06', ?, 96, ?, 88, 0, 70, 5, '2026-07-06 15:00:00')",
            [code, limit_up],
        )
    con.execute("CREATE TABLE sector_ranking(date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER, fetched_at TIMESTAMP)")
    for code in ["801001", "801002", "801003"]:
        con.execute("INSERT INTO sector_ranking VALUES ('2026-07-06', ?, ?, 30, '2026-07-06 15:00:00')", [code, code])
    con.execute(
        "CREATE TABLE sector_capital("
        "date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, "
        "big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT, fetched_at TIMESTAMP)"
    )
    for code, inflow in [("801001", 900000000), ("801002", 700000000), ("801003", 500000000)]:
        con.execute(
            "INSERT INTO sector_capital VALUES ('2026-07-06', ?, ?, 1, 1, 1, 1, '2026-07-06 15:00:00')",
            [code, inflow],
        )
    con.execute(
        "CREATE TABLE sector_stocks("
        "date DATE, sector_code VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "change_pct DOUBLE, turnover BIGINT, market_cap BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_stocks VALUES ('2026-07-06','801001','000001','Alpha',6.0,100000000,1000000000,'2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE l2_realtime_all_boards("
        "date DATE, board_level INTEGER, stock_code VARCHAR, stock_name VARCHAR, limit_up_time VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO l2_realtime_all_boards VALUES ('2026-07-06', 2, '000001', 'Alpha', '09:35', '2026-07-06 15:00:00')")
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO kline VALUES ('2026-07-06','000001',9.5,10.1,9.4,10.0,100000,1000000,6.0,'D','2026-07-06 15:00:00')")
    con.execute(
        "CREATE TABLE advanced_morning_bidding_summary("
        "date DATE, total_amount BIGINT, limit_up_count INTEGER, limit_down_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO advanced_morning_bidding_summary VALUES ('2026-07-06', 100000000, 2, 6, '2026-07-06 09:25:00')")
    con.close()


def test_acute_drop_risk_is_normalized_to_100_point_scale(tmp_path):
    db_path = tmp_path / "risk_scale.duckdb"
    _calibration_db(db_path, weak_market=True)
    build_normalized_views(db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        score = con.execute("SELECT acute_drop_risk_score FROM v_market_state_inputs").fetchone()[0]
    finally:
        con.close()

    assert 0 <= score <= 100


def test_sector_scores_do_not_saturate_all_high_quality_sectors(tmp_path):
    db_path = tmp_path / "sector_scale.duckdb"
    _calibration_db(db_path)

    generate_signals(db_path, "2026-07-06")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        scores = [row[0] for row in con.execute("SELECT score FROM sector_rotation_score ORDER BY score DESC").fetchall()]
    finally:
        con.close()

    assert len(scores) == 3
    assert max(scores) < 100
    assert len(set(round(score, 2) for score in scores)) > 1


def test_weak_market_penalizes_candidates_and_records_fallback_risk(tmp_path):
    db_path = tmp_path / "weak_market_candidates.duckdb"
    _calibration_db(db_path, weak_market=True)

    generate_signals(db_path, "2026-07-06")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        score, evidence_json = con.execute(
            "SELECT score, evidence_json FROM stock_candidate_score WHERE stock_code='000001'"
        ).fetchone()
    finally:
        con.close()
    evidence = json.loads(evidence_json)

    assert score < 65
    assert evidence["score_components"]["regime_penalty"] < 0
    assert any("fallback" in item.lower() for item in evidence["risk_points"])
