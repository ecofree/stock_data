import duckdb

from trade_system.normalize import build_normalized_views


def test_build_normalized_views_exposes_fallback_sources_for_professional_data_chains(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE advanced_morning_bidding_summary("
        "date DATE, total_amount BIGINT, limit_up_count INTEGER, "
        "limit_down_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO advanced_morning_bidding_summary VALUES "
        "('2026-07-06', 320000000, 6, 1, '2026-07-06 09:25:00')"
    )
    con.execute(
        "CREATE TABLE sector_strength("
        "date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, "
        "fengban_rate DOUBLE, dieting INTEGER, up_count INTEGER, down_count INTEGER, "
        "raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_strength VALUES "
        "('2026-07-06', '801001', 82, 7, 66, 0, 33, 4, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_ranking("
        "date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER, "
        "fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_ranking VALUES "
        "('2026-07-06', '801001', 'test sector', 20, '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_capital("
        "date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, "
        "big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE advanced_gujia_kline("
        "date DATE, stock_code VARCHAR, gujia_value DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO advanced_gujia_kline VALUES "
        "('2026-07-06', '000001', 9.8, 'D', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, "
        "fall_count INTEGER, consecutive_count INTEGER, raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES "
        "('2026-07-06', 55, 5, 3200, 900, 6, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE market_rise_fall("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, broken_limit_up_count INTEGER, "
        "blown_limit_up_count INTEGER, blown_limit_up_rate FLOAT, raw_field_5 INTEGER, raw_json VARCHAR, updated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_rise_fall VALUES "
        "('2026-07-06', 55, 5, 8, 12, 18.5, NULL, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE market_emotion_money("
        "date DATE, cgl DOUBLE, yll DOUBLE, success_rate DOUBLE, raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_emotion_money VALUES "
        "('2026-07-06', 71, 64, 62, '{}', '2026-07-06 15:00:00')"
    )
    con.execute("CREATE TABLE daily_new_high(date DATE, count INTEGER, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO daily_new_high VALUES ('2026-07-06', 38, '2026-07-06 15:00:00')")
    con.close()

    build_normalized_views(str(db_path))

    con = duckdb.connect(str(db_path))
    auction = con.execute(
        "SELECT source_table, is_fallback, auction_strength FROM v_auction_status"
    ).fetchone()
    sector = con.execute(
        "SELECT source_table, is_fallback, sector_name, strength_value FROM v_sector_capital"
    ).fetchone()
    kline = con.execute(
        "SELECT source_table, is_fallback, close FROM v_kline_daily"
    ).fetchone()
    market = con.execute(
        "SELECT earning_effect_score, acute_drop_risk_score, source_table, is_fallback "
        "FROM v_market_state_inputs"
    ).fetchone()
    index_state = con.execute(
        "SELECT source_table, is_fallback, index_code FROM v_index_state"
    ).fetchone()
    con.close()

    assert auction == ("advanced_morning_bidding_summary", True, 11.2)
    assert sector == ("sector_strength", True, "test sector", 82.0)
    assert kline == ("advanced_gujia_kline", True, 9.8)
    assert market[0] > 0
    assert market[1] > 0
    assert market[2:] == ("daily_summary+market_rise_fall+market_emotion_money", False)
    assert index_state == ("market_state_fallback", True, "MARKET_PROXY")


def test_build_normalized_views_prefers_required_sources_when_populated(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE auction_bidding_anomaly("
        "date DATE, stock_code VARCHAR, anomaly_type VARCHAR, anomaly_value DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO auction_bidding_anomaly VALUES "
        "('2026-07-06', '000001', 'high_amount', 12.5, '2026-07-06 09:25:00')"
    )
    con.execute(
        "CREATE TABLE sector_capital("
        "date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, "
        "big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_capital VALUES "
        "('2026-07-06', '801001', 500000000, 120000000, 90000000, 60000000, -20000000, '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO kline VALUES "
        "('2026-07-06', '000001', 9.5, 10.1, 9.4, 10.0, 100000, 1000000, 5.5, 'D', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE index_kline("
        "date VARCHAR, index_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO index_kline VALUES "
        "('2026-07-06', '000001.SH', 3000, 3050, 2990, 3040, 1000000, 2000000, 1.2, 'D', '{}', '2026-07-06 15:00:00')"
    )
    con.close()

    build_normalized_views(str(db_path))

    con = duckdb.connect(str(db_path))
    auction = con.execute("SELECT source_table, is_fallback FROM v_auction_status").fetchone()
    sector = con.execute("SELECT source_table, is_fallback, main_net_inflow FROM v_sector_capital").fetchone()
    kline = con.execute("SELECT source_table, is_fallback, close FROM v_kline_daily").fetchone()
    index_state = con.execute("SELECT source_table, is_fallback, close FROM v_index_state").fetchone()
    con.close()

    assert auction == ("auction_bidding_anomaly", False)
    assert sector == ("sector_capital", False, 500000000)
    assert kline == ("kline", False, 10.0)
    assert index_state == ("index_kline", False, 3040.0)


def test_index_state_uses_realtime_index_list_before_market_proxy(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE l2_realtime_index_list("
        "date DATE, index_code VARCHAR, index_name VARCHAR, price DOUBLE, change_pct DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_realtime_index_list VALUES "
        "('2026-07-06', 'SH000001', '上证指数', 3990.24, -1.26, '2026-07-06 15:00:00')"
    )
    con.close()

    build_normalized_views(str(db_path))

    con = duckdb.connect(str(db_path))
    row = con.execute("SELECT source_table, is_fallback, index_code, close FROM v_index_state").fetchone()
    con.close()
    assert row == ("l2_realtime_index_list", False, "SH000001", 3990.24)


def test_kline_view_normalizes_period_case_and_keeps_latest_row(tmp_path):
    db_path = tmp_path / "kline_case.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO kline VALUES "
        "('2026-07-06','000001',9,10,9,9.8,100,1000,-2,'d','2026-07-06 14:00:00'),"
        "('2026-07-06','000001',9,11,9,10.5,200,2000,5,'D','2026-07-06 15:00:00')"
    )
    con.close()

    build_normalized_views(str(db_path))

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT trade_date, stock_code, close, ktype FROM v_kline_daily"
        ).fetchall()
    finally:
        con.close()

    assert rows == [("2026-07-06", "000001", 10.5, "D")]
