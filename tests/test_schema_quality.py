import duckdb

from trade_system.quality import find_duplicate_keys, run_quality_audit
from trade_system.schema_audit import audit_schema, parse_defined_tables


def test_parse_defined_tables_extracts_create_table_names():
    text = (
        "CREATE TABLE IF NOT EXISTS market_mood (date DATE); "
        "CREATE TABLE IF NOT EXISTS sector_capital (date DATE);"
    )
    assert parse_defined_tables(text) == ["market_mood", "sector_capital"]


def test_audit_schema_reports_missing_and_extra_tables(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE market_mood(date DATE)")
    con.execute("CREATE TABLE extra_table(id INTEGER)")
    con.close()
    result = audit_schema(str(db_path), ["market_mood", "sector_capital"])
    assert result["missing_defined"] == ["sector_capital"]
    assert result["extra_actual"] == ["extra_table"]


def test_find_duplicate_keys_counts_duplicate_groups(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE market_rise_fall(date DATE, value INTEGER)")
    con.execute(
        "INSERT INTO market_rise_fall VALUES "
        "('2026-07-06', 1), ('2026-07-06', 2), ('2026-07-05', 1)"
    )
    con.close()
    result = find_duplicate_keys(str(db_path), "market_rise_fall", ["date"])
    assert result["duplicate_groups"] == 1
    assert result["max_duplicate_count"] == 2


def test_quality_audit_detects_case_normalized_kline_and_stage_duplicates(tmp_path):
    db_path = tmp_path / "quality.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR, ktype VARCHAR)")
    con.execute(
        "INSERT INTO kline VALUES ('2026-07-06','000001','d'),('2026-07-06','000001','D')"
    )
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR)"
    )
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-06','premarket_pool','000001'),"
        "('2026-07-06','premarket_pool','000001')"
    )
    con.execute("CREATE VIEW sample_view AS SELECT * FROM kline")
    con.close()

    result = run_quality_audit(db_path)
    by_table = {item["table"]: item for item in result["duplicates"]}

    assert by_table["kline"]["duplicate_groups"] == 1
    assert by_table["stock_candidate_stage_signal"]["duplicate_groups"] == 1
    assert result["summary"]["table_count"] == 2
    assert result["summary"]["view_count"] == 1
    assert result["summary"]["total_rows"] == 4
