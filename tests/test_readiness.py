import duckdb

from trade_system.readiness import assess_trade_date_readiness


def test_close_readiness_requires_same_date_capital_flows(tmp_path):
    db_path = tmp_path / "readiness.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO kline VALUES ('2026-07-09','000001')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-08','801001')")
    con.execute("CREATE TABLE l2_stock_intraday(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO l2_stock_intraday VALUES ('2026-07-09','000001')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "close")

    assert result["ready"] is False
    assert result["missing_groups"] == ["sector_capital_flow"]


def test_intraday_readiness_passes_when_both_capital_flows_are_current(tmp_path):
    db_path = tmp_path / "intraday.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-09','801001')")
    con.execute("CREATE TABLE l2_stock_bigorder(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO l2_stock_bigorder VALUES ('2026-07-09','000001')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "intraday")

    assert result["ready"] is True
    assert result["missing_groups"] == []


def test_intraday_readiness_uses_same_date_market_stock_flow(tmp_path):
    db_path = tmp_path / "market-flow.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-09','801001')")
    con.execute("CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-07-09','000001', current_timestamp)")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "intraday")

    assert result["ready"] is True
    stock_group = next(item for item in result["groups"] if item["group"] == "stock_capital_flow")
    assert stock_group["selected_relation"] == "multi_source_stock_flow"


def test_partial_market_batch_cannot_be_replaced_by_bounded_flow(tmp_path):
    db_path = tmp_path / "partial-market-flow.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE realtime_candidate_pool_snapshot(trade_date DATE, status VARCHAR, stock_count INTEGER)")
    con.execute("INSERT INTO realtime_candidate_pool_snapshot VALUES ('2026-07-09','success',2)")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-09','801001')")
    con.execute("CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-07-09','000001', current_timestamp)")
    con.execute("CREATE TABLE intraday_stock_flow_batch(trade_date DATE, expected_rows INTEGER, fetched_rows INTEGER, coverage_pct DOUBLE, status VARCHAR)")
    con.execute("INSERT INTO intraday_stock_flow_batch VALUES ('2026-07-09',5000,1,0.02,'partial')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "intraday")

    assert result["ready"] is False
    assert result["missing_groups"] == ["stock_capital_flow"]


def test_auction_accepts_calendar_proven_previous_session_market_context(tmp_path):
    db_path = tmp_path / "auction-previous-session.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)")
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES "
        "('2026-07-23', true), ('2026-07-24', true)"
    )
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-23')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-24' trade_date")
    con.execute("CREATE TABLE auction_tick(trade_date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO auction_tick VALUES ('2026-07-24', '000001')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-24", "auction")

    assert result["ready"] is True
    market = next(item for item in result["groups"] if item["group"] == "market_state")
    assert market["status"] == "previous_session_context"
    assert market["context_trade_date"] == "2026-07-23"


def test_auction_rejects_arbitrary_old_market_row(tmp_path):
    db_path = tmp_path / "auction-old-context.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)")
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES "
        "('2026-07-23', true), ('2026-07-24', true)"
    )
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-15')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-24' trade_date")
    con.execute("CREATE TABLE auction_tick(trade_date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO auction_tick VALUES ('2026-07-24', '000001')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-24", "auction")

    assert result["ready"] is False
    assert "market_state" in result["missing_groups"]
