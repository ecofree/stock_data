import duckdb

from trade_system.review_statistics import build_daily_review_statistics, render_daily_review_statistics


def test_daily_review_statistics_marks_small_stage_samples_as_insufficient(tmp_path):
    db_path = tmp_path / "review_stats.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "score DOUBLE, decision VARCHAR, evidence_json VARCHAR)"
    )
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR)"
    )
    for day, close in [("2026-07-06", 10.0), ("2026-07-07", 10.2)]:
        con.execute(
            "INSERT INTO kline VALUES (?, '000001', 10, 10, 10, ?, 100, 1000, 0, 'D')",
            [day, close],
        )
    con.execute(
        "CREATE TABLE market_regime_snapshot("
        "trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, evidence_json VARCHAR)"
    )
    con.execute("INSERT INTO market_regime_snapshot VALUES ('2026-07-06','weak',20,5,'{}')")
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-06','premarket_pool','000001','Alpha',80,'pool','{}')"
    )
    con.close()

    stats = build_daily_review_statistics(db_path, min_return_samples=5)
    report = render_daily_review_statistics(stats)

    stage = stats["stage_statistics"]["premarket_pool"]
    assert stage["return_sample_count"] == 1
    assert stage["verdict"] == "insufficient_sample"
    assert stats["regime_stage_counts"]["weak"]["premarket_pool"] == 1
    assert "insufficient_sample" in report
    assert "Daily Review Statistics" in report
