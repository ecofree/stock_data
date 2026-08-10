import csv

import duckdb

from trade_system.backfill import import_professional_csvs
from trade_system.backtest import run_stage_candidate_backtest
from trade_system.normalize import build_normalized_views
from trade_system.signals import generate_signals


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_professional_csv_backfill_populates_real_sources_and_is_idempotent(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    kline_csv = tmp_path / "kline.csv"
    index_csv = tmp_path / "index_kline.csv"
    sector_capital_csv = tmp_path / "sector_capital.csv"
    auction_csv = tmp_path / "auction_tick.csv"

    _write_csv(
        kline_csv,
        [
            {
                "date": "2026-07-06",
                "stock_code": "000001",
                "open": "9.5",
                "high": "10.1",
                "low": "9.4",
                "close": "10.0",
                "volume": "100000",
                "turnover": "1000000",
                "change_pct": "5.5",
                "ktype": "D",
            }
        ],
    )
    _write_csv(
        index_csv,
        [
            {
                "date": "2026-07-06",
                "index_code": "000001.SH",
                "open": "3000",
                "high": "3050",
                "low": "2990",
                "close": "3040",
                "volume": "1000000",
                "turnover": "2000000",
                "change_pct": "1.2",
                "ktype": "D",
            }
        ],
    )
    _write_csv(
        sector_capital_csv,
        [
            {
                "date": "2026-07-06",
                "sector_code": "801001",
                "main_net_inflow": "500000000",
                "super_net_inflow": "120000000",
                "big_net_inflow": "90000000",
                "mid_net_inflow": "60000000",
                "small_net_inflow": "-20000000",
            }
        ],
    )
    _write_csv(
        auction_csv,
        [
            {
                "date": "2026-07-06",
                "stock_code": "000001",
                "time": "09:25",
                "price": "10.0",
                "volume": "50000",
            }
        ],
    )

    first = import_professional_csvs(
        str(db_path),
        {
            "kline": str(kline_csv),
            "index_kline": str(index_csv),
            "sector_capital": str(sector_capital_csv),
            "auction_tick": str(auction_csv),
        },
    )
    second = import_professional_csvs(str(db_path), {"kline": str(kline_csv)})

    assert first["kline"] == 1
    assert first["index_kline"] == 1
    assert first["sector_capital"] == 1
    assert first["auction_tick"] == 1
    assert second["kline"] == 1

    build_normalized_views(str(db_path))
    con = duckdb.connect(str(db_path))
    assert con.execute("SELECT count(*) FROM kline").fetchone()[0] == 1
    assert con.execute("SELECT source_table, is_fallback FROM v_kline_daily").fetchone() == ("kline", False)
    assert con.execute("SELECT source_table, is_fallback FROM v_index_state").fetchone() == ("index_kline", False)
    assert con.execute("SELECT source_table, is_fallback FROM v_sector_capital").fetchone() == ("sector_capital", False)
    assert con.execute("SELECT source_table, is_fallback FROM v_auction_status").fetchone() == ("auction_tick", False)
    con.close()


def test_generate_signals_builds_context_without_rewriting_stage_signals(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, "
        "fall_count INTEGER, consecutive_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-06', 50, 6, 3000, 1000, 5, '2026-07-06 15:00:00')")
    con.execute(
        "CREATE TABLE sector_strength("
        "date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, "
        "fengban_rate DOUBLE, dieting INTEGER, up_count INTEGER, down_count INTEGER, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO sector_strength VALUES ('2026-07-06', '801001', 80, 8, 70, 0, 40, 5, '2026-07-06 15:00:00')")
    con.execute("CREATE TABLE sector_ranking(date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO sector_ranking VALUES ('2026-07-06', '801001', 'test sector', 20, '2026-07-06 15:00:00')")
    con.execute(
        "CREATE TABLE sector_capital("
        "date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, "
        "big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-06', '801001', 500000000, 100000000, 80000000, 50000000, -10000000, '2026-07-06 15:00:00')")
    con.execute(
        "CREATE TABLE sector_stocks("
        "date DATE, sector_code VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "change_pct DOUBLE, turnover BIGINT, market_cap BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO sector_stocks VALUES ('2026-07-06', '801001', '000001', 'test stock', 5.5, 1000000, 1000000000, '2026-07-06 15:00:00')")
    con.execute(
        "CREATE TABLE l2_realtime_all_boards("
        "date DATE, board_level INTEGER, stock_code VARCHAR, stock_name VARCHAR, limit_up_time VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO l2_realtime_all_boards VALUES ('2026-07-06', 2, '000001', 'test stock', '09:35', '2026-07-06 15:00:00')")
    con.execute("CREATE TABLE auction_bidding_anomaly(date DATE, stock_code VARCHAR, anomaly_type VARCHAR, anomaly_value DOUBLE, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO auction_bidding_anomaly VALUES ('2026-07-06', '000001', 'high_amount', 12.5, '2026-07-06 09:25:00')")
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO kline VALUES ('2026-07-06', '000001', 9.5, 10.1, 9.4, 10.0, 100000, 1000000, 5.5, 'D', '2026-07-06 15:00:00')")
    con.execute("CREATE TABLE l2_stock_intraday(date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE, avg_price DOUBLE, volume BIGINT, turnover BIGINT, main_fund_net BIGINT, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO l2_stock_intraday VALUES ('2026-07-06', '000001', '10:30', 10.0, 9.8, 50000, 500000, 20000000, '2026-07-06 10:30:00')")
    con.execute("CREATE TABLE l2_stock_bigorder(date DATE, stock_code VARCHAR, time VARCHAR, big_net_amount BIGINT, big_buy BIGINT, big_sell BIGINT, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO l2_stock_bigorder VALUES ('2026-07-06', '000001', '10:30', 30000000, 50000000, 20000000, '2026-07-06 10:30:00')")
    con.execute("CREATE TABLE l2_tick_history(date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE, volume BIGINT, direction VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO l2_tick_history VALUES ('2026-07-06', '000001', '10:30', 10.0, 1000, 'buy', '2026-07-06 10:30:00')")
    con.execute("CREATE TABLE l2_tick_orders(date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE, volume BIGINT, order_type VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO l2_tick_orders VALUES ('2026-07-06', '000001', '10:31', 10.0, 2000, 'buy', '2026-07-06 10:31:00')")
    con.execute("CREATE TABLE l2_tick_orders_all(date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE, volume BIGINT, order_type VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO l2_tick_orders_all VALUES ('2026-07-06', '000001', '10:32', 10.0, 3000, 'buy', '2026-07-06 10:32:00')")
    con.execute(
        "CREATE TABLE advanced_pankou("
        "date DATE, stock_code VARCHAR, buy1_price DOUBLE, buy1_volume BIGINT, "
        "sell1_price DOUBLE, sell1_volume BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO advanced_pankou VALUES ('2026-07-06', '000001', 9.99, 9000, 10.0, 4000, '2026-07-06 10:32:00')")
    con.close()

    result = generate_signals(str(db_path), "2026-07-06")

    con = duckdb.connect(str(db_path))
    stage_count = con.execute("SELECT count(*) FROM stock_candidate_stage_signal").fetchone()[0]
    candidate_count = con.execute("SELECT count(*) FROM stock_candidate_score").fetchone()[0]
    con.close()
    assert result["stage_candidate_count"] == 0
    assert stage_count == 0
    assert candidate_count == 1


def test_stage_candidate_backtest_returns_stage_statistics(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)"
    )
    for stage in ["premarket_pool", "auction_confirmation", "intraday_strength", "close_decision"]:
        con.execute(
            "INSERT INTO stock_candidate_stage_signal VALUES "
            "('2026-07-06', ?, '000001', 'test stock', 80, 'watch', '{}', '2026-07-06 15:00:00')",
            [stage],
        )
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute("INSERT INTO kline VALUES ('2026-07-06', '000001', 9.5, 10.1, 9.4, 10.0, 100000, 1000000, 5.5, 'D', '2026-07-06 15:00:00')")
    con.execute("INSERT INTO kline VALUES ('2026-07-07', '000001', 10.0, 10.8, 9.9, 10.5, 110000, 1200000, 5.0, 'D', '2026-07-07 15:00:00')")
    con.close()
    build_normalized_views(str(db_path))

    result = run_stage_candidate_backtest(str(db_path))

    assert result["sample_count"] == 4
    assert result["stage_counts"]["premarket_pool"] == 1
    assert result["stage_stats"]["auction_confirmation"]["hit_count"] == 1
    assert result["rows"][0]["forward_return_pct"] == 5.26
    assert result["return_sample_count"] == 3
    assert result["independent_sample_count"] == 1
    assert result["excluded_count"] == 1
