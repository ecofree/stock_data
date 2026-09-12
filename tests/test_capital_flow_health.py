import duckdb
from datetime import datetime

from trade_system.capital_flow_health import assess_capital_flow_health


def test_explicit_ttl_is_not_disabled_by_selecting_old_trade_date(tmp_path):
    db = tmp_path/'ttl.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP)')
        con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-09-11','000001',100,'2026-09-11 15:00:00')")
    expired = assess_capital_flow_health(db,'2026-09-11',max_age_seconds=7200,now=datetime(2026,9,12,10))
    assert expired['effective_max_age_seconds'] == 7200
    assert expired['stock_flow']['observed_codes'] == 0
    historical = assess_capital_flow_health(db,'2026-09-11',max_age_seconds=7200,now=datetime(2026,9,11,16))
    assert historical['stock_flow']['observed_codes'] == 1


def test_stale_and_nonfinite_rows_do_not_count_as_usable_flow():
    from trade_system.capital_flow_health import _relation_health
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP, is_stale BOOLEAN)')
        con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-09-11','A',100,'2026-09-11 15:00:00',true), ('2026-09-11','B','NaN','2026-09-11 15:00:00',false)")
        result = _relation_health(con,'multi_source_stock_flow','2026-09-11','stock_code',now=datetime(2026,9,11,16))
        assert result['codes'] == 0


def test_capital_flow_historical_as_of_rejects_future_writes(tmp_path):
    db_path = tmp_path / "future-flow.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow("
        "source_date DATE,stock_code VARCHAR,main_net DOUBLE,fetched_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE multi_source_sector_flow("
        "source_date DATE,sector_code VARCHAR,main_net DOUBLE,fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "('2026-07-31','000001',100,'2026-08-01 09:00:00')"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES "
        "('2026-07-31','BK1',100,'2026-08-01 09:00:00')"
    )
    con.close()

    result = assess_capital_flow_health(
        db_path,
        "2026-07-31",
        1,
        1,
        max_age_seconds=7200,
        now=datetime.fromisoformat("2026-07-31T17:45:00"),
    )

    assert result["ready"] is False
    assert result["stock_flow"]["observed_codes"] == 0
    assert result["sector_flow"]["observed_codes"] == 0


def test_capital_flow_aware_as_of_is_comparable_to_naive_db_time(tmp_path):
    db_path = tmp_path / "aware.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow("
        "source_date DATE, stock_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "('2026-07-31','000001',100,'2026-07-31 17:45:00')"
    )
    con.close()
    result = assess_capital_flow_health(
        db_path,
        "2026-07-31",
        expected_stock_codes=1,
        min_coverage_pct=99.5,
        max_age_seconds=1200,
        now=datetime.fromisoformat("2026-07-31T18:00:00+08:00"),
    )
    assert result["stock_flow"]["ready"] is True


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


def test_capital_flow_health_rejects_count_complete_partial_sector_batch(tmp_path):
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

    assert result["sector_flow"]["ready"] is False
    assert result["ready"] is False
