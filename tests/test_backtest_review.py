import duckdb

from trade_system.backtest import run_market_regime_backtest
from trade_system.review import render_daily_report


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


def test_render_daily_report_contains_core_sections():
    report = render_daily_report(
        trade_date="2026-07-06",
        quality={"summary": {"table_count": 2, "total_rows": 10}},
        regime={"regime": "主升", "suggested_position_pct": 70},
        sectors=[{"sector_name": "test sector", "score": 88}],
        candidates=[{"stock_name": "test stock", "score": 77}],
        alerts=[{"severity": "P1", "message": "test alert"}],
        backtest={"sample_count": 2, "regime_counts": {"主升": 1}},
    )

    assert "盘前/盘后交易辅助报告" in report
    assert "市场状态" in report
    assert "风险告警" in report
