import duckdb

from trade_system.operator_backtest import run_operator_stage_backtest


def test_operator_stage_backtest_applies_t1_fees_and_slippage(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-06', 'close_decision', '000001', 'test stock', 90, 'watch', '{}', '2026-07-06')"
    )
    con.execute(
        "CREATE TABLE kline("
        "date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO kline VALUES "
        "('2026-07-06', '000001', 10, 10, 10, 10, 1000, 10000, 0, 'D', '2026-07-06')"
    )
    con.execute(
        "INSERT INTO kline VALUES "
        "('2026-07-07', '000001', 10.0, 11.0, 9.8, 10.5, 1000, 10500, 5, 'D', '2026-07-07')"
    )
    con.close()

    result = run_operator_stage_backtest(db_path, fee_rate=0.001, slippage_bps=10)

    assert result["sample_count"] == 1
    row = result["rows"][0]
    assert row["entry_date"] == "2026-07-07"
    assert row["gross_return_pct"] == 5.0
    assert row["net_return_pct"] < 5.0


def test_operator_stage_backtest_accepts_lowercase_daily_kline_type(tmp_path):
    db_path = tmp_path / "lowercase.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE)"
    )
    con.execute("INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-06','close_decision','000001','test stock',80)")
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR, open DOUBLE, close DOUBLE, ktype VARCHAR)")
    con.execute("INSERT INTO kline VALUES ('2026-07-06','000001',9.8,10,'d')")
    con.execute("INSERT INTO kline VALUES ('2026-07-07','000001',10.0,10.5,'d')")
    con.close()

    result = run_operator_stage_backtest(db_path)

    assert result["sample_count"] == 1
    assert result["rows"][0]["source"] == "proxy_signal"
