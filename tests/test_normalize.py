import duckdb

from trade_system.normalize import build_normalized_views


def test_build_normalized_views_dedupes_market_daily(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, "
        "rise_count INTEGER, fall_count INTEGER, consecutive_count INTEGER, "
        "raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES "
        "('2026-07-06', 10, 2, 3000, 1000, 4, '{}', '2026-07-06 09:00:00'), "
        "('2026-07-06', 11, 3, 3100, 900, 5, '{}', '2026-07-06 15:00:00')"
    )
    con.close()

    build_normalized_views(str(db_path))

    con = duckdb.connect(str(db_path))
    rows = con.execute("SELECT trade_date, limit_up_count FROM v_market_daily").fetchall()
    con.close()
    assert rows == [("2026-07-06", 11)]


def test_market_fallback_flag_survives_normalized_views(tmp_path):
    db_path = tmp_path / "fallback-market.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, "
        "rise_count INTEGER, fall_count INTEGER, consecutive_count INTEGER, "
        "raw_json VARCHAR, fetched_at TIMESTAMP, source_kind VARCHAR)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES "
        "('2026-07-24',10,2,3000,1000,4,'{}','2026-07-24 09:00:00','fallback')"
    )
    con.close()

    build_normalized_views(str(db_path))

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert con.execute(
            "SELECT is_fallback FROM v_market_daily"
        ).fetchone()[0] is True
        assert con.execute(
            "SELECT is_fallback FROM v_market_state_inputs"
        ).fetchone()[0] is True
    finally:
        con.close()
