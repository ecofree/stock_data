import duckdb

from trade_system.capital_flow_health import assess_capital_flow_health


def test_capital_flow_health_requires_real_sector_capital(tmp_path):
    db_path = tmp_path / "capital.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE l2_stock_intraday("
        "date DATE, stock_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_stock_intraday VALUES "
        "('2026-07-09','000001','2026-07-09 10:00:00')"
    )
    con.execute(
        "CREATE TABLE l2_sector_intraday("
        "date DATE, sector_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_sector_intraday VALUES "
        "('2026-07-09','801001','2026-07-09 10:00:00')"
    )
    con.close()

    result = assess_capital_flow_health(db_path, "2026-07-09", 1, 1)

    assert result["stock_flow"]["ready"] is True
    assert result["sector_flow"]["ready"] is False
    assert result["ready"] is False

    current_run = assess_capital_flow_health(
        db_path,
        "2026-07-09",
        1,
        1,
        collected_after="2026-07-09T10:01:00",
    )
    assert current_run["stock_flow"]["ready"] is False
    stock = next(
        item
        for item in current_run["stock_flow"]["relations"]
        if item["relation"] == "l2_stock_intraday"
    )
    assert stock["status"] == "old_for_date"
    assert stock["recent_rows"] == 0


def test_capital_flow_health_enforces_expected_coverage(tmp_path):
    db_path = tmp_path / "coverage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE l2_stock_intraday(date DATE, stock_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_stock_intraday VALUES "
        "('2026-07-09','000001','2026-07-09 10:00:00'),"
        "('2026-07-09','000002','2026-07-09 10:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_capital(date DATE, sector_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_capital VALUES "
        "('2026-07-09','801001','2026-07-09 10:00:00')"
    )
    con.close()

    result = assess_capital_flow_health(db_path, "2026-07-09", 2, 2)

    assert result["stock_flow"]["ready"] is True
    assert result["sector_flow"]["coverage_pct"] == 50.0
    assert result["sector_flow"]["ready"] is False
    assert result["ready"] is False


def test_capital_flow_health_accepts_fresh_migrated_flow_rows(tmp_path):
    db_path = tmp_path / "migrated-flow.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, fetched_at TIMESTAMP, is_stale BOOLEAN)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "('2026-07-14','000001','2026-07-14 10:00:00',FALSE),"
        "('2026-07-14','000002','2026-07-14 10:00:00',FALSE)"
    )
    con.execute(
        "CREATE TABLE multi_source_sector_flow(source_date DATE, sector_code VARCHAR, fetched_at TIMESTAMP, is_stale BOOLEAN)"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES "
        "('2026-07-14','801001','2026-07-14 10:00:00',FALSE),"
        "('2026-07-14','801002','2026-07-14 10:00:00',FALSE)"
    )
    con.close()

    result = assess_capital_flow_health(
        db_path, "2026-07-14", 2, 2, collected_after="2026-07-14T09:00:00"
    )

    assert result["stock_flow"]["ready"] is True
    assert result["sector_flow"]["ready"] is True
    assert result["ready"] is True


def test_capital_flow_health_accepts_complete_partial_sector_batch(tmp_path):
    db_path = tmp_path / "complete-partial-sector.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES ('2026-07-16','000001',100,'2026-07-16 10:00:00')"
    )
    con.execute(
        "CREATE TABLE multi_source_sector_flow(source_date DATE, sector_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES ('2026-07-16','BK0001',100,'2026-07-16 10:00:00')"
    )
    con.execute(
        "CREATE TABLE intraday_stock_flow_batch(trade_date DATE, expected_rows INTEGER)"
    )
    con.execute("INSERT INTO intraday_stock_flow_batch VALUES ('2026-07-16', 1)")
    con.execute(
        "CREATE TABLE intraday_sector_flow_batch(trade_date DATE, expected_rows INTEGER, fetched_rows INTEGER, coverage_pct DOUBLE, status VARCHAR)"
    )
    con.execute(
        "INSERT INTO intraday_sector_flow_batch VALUES ('2026-07-16', 1, 1, 100, 'partial')"
    )
    con.close()

    result = assess_capital_flow_health(db_path, "2026-07-16")

    assert result["sector_flow"]["ready"] is True
    assert result["ready"] is True
