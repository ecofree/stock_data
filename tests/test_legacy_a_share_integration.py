import duckdb

from trade_system.integration.legacy_a_share import audit_legacy_project


def test_audit_legacy_project_reports_tables_counts_and_import_plan(tmp_path):
    legacy_root = tmp_path / "legacy"
    db_dir = legacy_root / "db"
    db_dir.mkdir(parents=True)
    legacy_db = db_dir / "kpl_qds.duckdb"
    con = duckdb.connect(str(legacy_db))
    con.execute("CREATE TABLE daily_watchlist(date DATE, stock_code VARCHAR, final_score DOUBLE)")
    con.execute("INSERT INTO daily_watchlist VALUES ('2026-07-05', '000001', 88.5)")
    con.execute("CREATE TABLE auction_snapshots(date DATE, stock_code VARCHAR)")
    con.close()

    result = audit_legacy_project(legacy_root)

    assert result["legacy_root"] == str(legacy_root)
    assert result["database"]["exists"] is True
    assert result["tables"]["daily_watchlist"]["row_count"] == 1
    assert result["tables"]["auction_snapshots"]["row_count"] == 0
    assert "daily_watchlist" in result["import_tables"]
    assert "qmt_bridge.py" in result["discard_files"]


def test_import_legacy_tables_copies_selected_tables_with_prefix(tmp_path):
    stock_db = tmp_path / "stock.duckdb"
    legacy_root = tmp_path / "legacy"
    db_dir = legacy_root / "db"
    db_dir.mkdir(parents=True)
    legacy_db = db_dir / "kpl_qds.duckdb"
    con = duckdb.connect(str(legacy_db))
    con.execute("CREATE TABLE daily_watchlist(date DATE, stock_code VARCHAR, final_score DOUBLE)")
    con.execute("INSERT INTO daily_watchlist VALUES ('2026-07-05', '000001', 88.5)")
    con.execute("CREATE TABLE sector_strength(date DATE, sector_name VARCHAR, strength_score DOUBLE)")
    con.execute("INSERT INTO sector_strength VALUES ('2026-07-05', 'test sector', 77.0)")
    con.close()

    from trade_system.integration.legacy_a_share import import_legacy_tables

    result = import_legacy_tables(stock_db, legacy_root)

    assert result["legacy_qds_daily_watchlist"] == 1
    assert result["legacy_qds_sector_strength"] == 1
    con = duckdb.connect(str(stock_db))
    row = con.execute("SELECT stock_code, final_score, legacy_source_table FROM legacy_qds_daily_watchlist").fetchone()
    con.close()
    assert row == ("000001", 88.5, "daily_watchlist")
