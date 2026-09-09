import duckdb

from trade_system.empty_table_catalog import build_empty_table_catalog, render_empty_table_catalog


def test_empty_table_catalog_classifies_empty_tables_by_endpoint_status(tmp_path):
    db_path = tmp_path / "empty_catalog.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE auction_tick(date DATE)")
    con.execute("CREATE TABLE news_theme(date DATE)")
    con.execute("CREATE TABLE qlib_prediction(symbol VARCHAR)")
    con.execute("CREATE TABLE operator_trade_outcome(trade_date VARCHAR)")
    con.execute("CREATE TABLE kline(date DATE)")
    con.execute("INSERT INTO kline VALUES ('2026-07-06')")
    con.execute(
        "CREATE TABLE api_endpoint_inventory("
        "endpoint VARCHAR, table_name VARCHAR, table_rows INTEGER, verdict VARCHAR, usefulness VARCHAR)"
    )
    con.execute(
        "INSERT INTO api_endpoint_inventory VALUES "
        "('/auction/tick','auction_tick',0,'needs_trading_session','professional_core'),"
        "('/news/theme','news_theme',0,'api_available','professional_useful')"
    )
    con.execute(
        "CREATE TABLE api_endpoint_probe_run("
        "run_id VARCHAR PRIMARY KEY,base_url VARCHAR,probe_date DATE,"
        "endpoint_count INTEGER,status VARCHAR,started_at TIMESTAMP,"
        "completed_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO api_endpoint_probe_run VALUES "
        "('test-run','https://kpl-api.cn', '2026-07-06', 2, 'completed', now(), now())"
    )
    con.close()

    catalog = build_empty_table_catalog(db_path)
    by_table = {item["table_name"]: item for item in catalog["items"]}
    report = render_empty_table_catalog(catalog)

    assert by_table["auction_tick"]["classification"] == "needs_trading_session"
    assert by_table["news_theme"]["classification"] == "api_available_not_collected"
    assert by_table["qlib_prediction"]["classification"] == "external_missing"
    assert by_table["operator_trade_outcome"]["classification"] == "workflow_not_used"
    assert "kline" not in by_table
    assert "Empty Table Catalog" in report
    assert "api_available_not_collected" in report
