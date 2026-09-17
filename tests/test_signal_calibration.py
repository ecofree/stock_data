import duckdb

from trade_system.normalize import build_normalized_views


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
