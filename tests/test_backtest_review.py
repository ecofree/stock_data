import duckdb

from trade_system.backtest import run_market_regime_backtest


def test_backtest_returns_sample_count(tmp_path):
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
        "('2026-07-05', 10, 40, 1000, 3500, 2, '{}', '2026-07-05 15:00:00'), "
        "('2026-07-06', 80, 5, 3600, 900, 7, '{}', '2026-07-06 15:00:00')"
    )
    con.close()

    result = run_market_regime_backtest(str(db_path))

    assert result["sample_count"] == 2
    assert "regime_counts" in result
