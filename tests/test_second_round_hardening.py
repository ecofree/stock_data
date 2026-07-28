import json

import duckdb

from trade_system.backfill import derive_index_kline_from_daily_summary


def test_derive_index_kline_from_daily_summary_populates_fallback_index_rows(tmp_path):
    db_path = tmp_path / "index.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE, raw_json VARCHAR)")
    con.execute(
        """
        INSERT INTO daily_summary VALUES
        ('2026-07-06', '{"上证指数":4041.24,"涨跌幅":"-0.06%","成交额":1432112821099}')
        """
    )
    con.close()

    result = derive_index_kline_from_daily_summary(db_path)
    result_again = derive_index_kline_from_daily_summary(db_path)

    assert result["source_rows"] == 1
    assert result["inserted_rows"] == 1
    assert result_again["inserted_rows"] == 1
    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT CAST(date AS VARCHAR), index_code, close, change_pct, turnover, ktype FROM index_kline"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("2026-07-06", "SH000001", 4041.24, -0.06, 1432112821099, "D")]

from trade_system.data_chain import assess_data_chains
from trade_system.normalize import build_normalized_views
from trade_system.quality import dedupe_table, find_duplicate_keys
from trade_system.signals import generate_signals


def test_dedupe_table_archives_old_duplicate_rows(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE market_mood("
        "date DATE, rise_count INTEGER, fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO market_mood VALUES "
        "('2026-07-06', 100, '2026-07-06 09:00:00', '{\"old\":true}'), "
        "('2026-07-06', 200, '2026-07-06 15:00:00', '{\"new\":true}')"
    )
    con.close()

    result = dedupe_table(str(db_path), "market_mood", ["date"])

    assert result["removed_rows"] == 1
    assert find_duplicate_keys(str(db_path), "market_mood", ["date"])["duplicate_groups"] == 0
    con = duckdb.connect(str(db_path))
    kept = con.execute("SELECT rise_count FROM market_mood").fetchone()[0]
    archived = con.execute("SELECT rise_count FROM _dedupe_archive_market_mood").fetchone()[0]
    con.close()
    assert kept == 200
    assert archived == 100


def test_assess_data_chains_marks_missing_and_fallback_sources(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE sector_strength(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_strength VALUES ('2026-07-06', '801001')")
    con.execute("CREATE TABLE l2_realtime_all_boards(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO l2_realtime_all_boards VALUES ('2026-07-06', '000001')")
    con.execute("CREATE TABLE l2_realtime_index_list(date DATE, index_code VARCHAR)")
    con.execute("INSERT INTO l2_realtime_index_list VALUES ('2026-07-06', 'SH000001')")
    con.execute("CREATE TABLE auction_bidding_anomaly(date DATE)")
    con.close()

    chains = assess_data_chains(str(db_path))

    by_name = {item["chain"]: item for item in chains}
    assert by_name["集合竞价"]["status"] == "missing"
    assert by_name["板块资金"]["status"] == "fallback"
    assert by_name["L2可用数据"]["status"] == "available"

def test_assess_data_chains_treats_realtime_index_list_as_index_source(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE l2_realtime_index_list(date DATE, index_code VARCHAR)")
    con.execute("INSERT INTO l2_realtime_index_list VALUES ('2026-07-06', 'SH000001')")
    con.close()

    chains = assess_data_chains(str(db_path))

    index_chain = next(item for item in chains if "index_kline" in item["required"])
    assert index_chain["status"] == "available"
    assert index_chain["required_present"] == ["l2_realtime_index_list"]


def test_assess_data_chains_marks_historical_rows_stale_for_requested_date(tmp_path):
    db_path = tmp_path / "stale.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-08','801001')")
    con.close()

    chains = assess_data_chains(str(db_path), "2026-07-09")

    sector = next(item for item in chains if item["chain"] == "板块资金")
    assert sector["status"] == "stale"
    assert sector["latest_dates"]["sector_capital"] == "2026-07-08"


def test_generate_signals_writes_explainable_evidence(tmp_path):
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
        "('2026-07-06', 55, 8, 3000, 1100, 6, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES "
        "('2026-07-05', 35, 6, 2400, 1600, 4, '{}', '2026-07-05 15:00:00')"
    )
    con.execute(
        "CREATE TABLE market_rise_fall("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, broken_limit_up_count INTEGER, "
        "blown_limit_up_count INTEGER, blown_limit_up_rate FLOAT, raw_field_5 INTEGER, raw_json VARCHAR, updated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_rise_fall VALUES "
        "('2026-07-06', 55, 8, 9, 11, 17.5, NULL, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE market_emotion_money("
        "date DATE, cgl DOUBLE, yll DOUBLE, success_rate DOUBLE, raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_emotion_money VALUES "
        "('2026-07-06', 68, 61, 59, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_strength("
        "date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, "
        "fengban_rate DOUBLE, dieting INTEGER, up_count INTEGER, down_count INTEGER, "
        "raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_strength VALUES "
        "('2026-07-06', '801001', 70, 8, 60, 0, 30, 8, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_ranking("
        "date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER, "
        "fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO sector_ranking VALUES "
        "('2026-07-06', '801001', 'test sector', 20, '2026-07-06 15:00:00', '{}')"
    )
    con.execute(
        "CREATE TABLE l2_realtime_all_boards("
        "date DATE, board_level INTEGER, stock_code VARCHAR, stock_name VARCHAR, "
        "limit_up_time VARCHAR, fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO l2_realtime_all_boards VALUES "
        "('2026-07-06', 2, '000001', 'test stock', '09:35', '2026-07-06 15:00:00', '{}')"
    )
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO kline VALUES "
        "('2026-07-06', '000001', 9.5, 10.1, 9.4, 10.0, 100000, 1000000, 5.5, 'D', '2026-07-06 15:00:00', '{}')"
    )
    con.execute(
        "CREATE TABLE advanced_morning_bidding_summary("
        "date DATE, total_amount BIGINT, limit_up_count INTEGER, limit_down_count INTEGER, fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO advanced_morning_bidding_summary VALUES "
        "('2026-07-06', 320000000, 6, 1, '2026-07-06 09:25:00', '{}')"
    )
    con.close()
    build_normalized_views(str(db_path))

    generate_signals(str(db_path), "2026-07-06")

    con = duckdb.connect(str(db_path))
    sector_evidence = con.execute(
        "SELECT evidence_json FROM sector_rotation_score LIMIT 1"
    ).fetchone()[0]
    stock_evidence = con.execute(
        "SELECT evidence_json FROM stock_candidate_score LIMIT 1"
    ).fetchone()[0]
    market_evidence = con.execute(
        "SELECT evidence_json FROM market_regime_snapshot LIMIT 1"
    ).fetchone()[0]
    con.close()
    market = json.loads(market_evidence)
    assert "sample_stats" in market
    assert "earning_effect_score" in market
    sector = json.loads(sector_evidence)
    assert "score_components" in sector
    assert "sample_stats" in sector
    assert sector["capital_source"]["source_table"] == "sector_strength"
    stock = json.loads(stock_evidence)
    assert stock["entry_reason"]
    assert stock["risk_points"]
    assert stock["invalidation"]
    assert stock["sample_stats"]
    assert stock["kline_filter"]["source_table"] == "kline"
    assert stock["auction_confirmation"]["source_table"] == "advanced_morning_bidding_summary"
