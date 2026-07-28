import duckdb

from trade_system.reports.real_data_backfill import build_real_data_backfill_status, render_real_data_backfill_report


def test_real_data_backfill_status_counts_sources_and_flags_history_gaps(tmp_path):
    db_path = tmp_path / "backfill_status.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    for idx in range(250):
        con.execute("INSERT INTO daily_summary VALUES (DATE '2026-07-06' - ?::INTEGER)", [idx])
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO kline VALUES ('2026-07-06','000001')")
    con.execute("CREATE TABLE index_kline(date VARCHAR, index_code VARCHAR)")
    con.execute("INSERT INTO index_kline VALUES ('2026-07-06','SH000001')")
    con.execute("CREATE TABLE auction_tick(date DATE, stock_code VARCHAR)")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-06','801001')")
    con.close()

    status = build_real_data_backfill_status(db_path)
    report = render_real_data_backfill_report(status)

    assert status["counts"]["kline"] == 1
    assert status["counts"]["index_kline"] == 1
    assert status["counts"]["auction_tick"] == 0
    assert status["history_coverage"]["kline"]["required_days"] == 250
    assert status["history_coverage"]["kline"]["observed_days"] == 1
    assert status["history_coverage"]["kline"]["status"] == "needs_backfill"
    assert status["history_coverage"]["sector_strength"]["status"] == "needs_external_source"
    assert "auction_tick" in status["gaps"][0]
    assert "# Real Data Backfill Status" in report
    assert "History Coverage" in report
    assert "needs_external_source" in report
    assert "auction_tick" in report
