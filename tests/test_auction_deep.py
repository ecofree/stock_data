import duckdb

from trade_system.auction_deep import analyze_auction_deep


def test_auction_deep_reports_degraded_when_tick_is_empty_but_anomaly_exists(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE auction_bidding_anomaly("
        "date DATE, stock_code VARCHAR, anomaly_type VARCHAR, anomaly_value DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO auction_bidding_anomaly VALUES "
        "('2026-07-06', '000001', 'bidding_amount', 100000000, '2026-07-06 09:25:00')"
    )
    con.close()

    result = analyze_auction_deep(db_path, "2026-07-06")

    assert result["mode"] == "degraded_anomaly"
    assert result["rows"][0]["stock_code"] == "000001"
    assert result["rows"][0]["reliability"] == "medium"
