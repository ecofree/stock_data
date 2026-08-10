import duckdb

from trade_system.integrity import repair_critical_integrity


def test_integrity_normalizes_and_deduplicates_ths_member_codes(tmp_path):
    db_path = tmp_path / "ths.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE ths_concept_stock_history("
        "trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,"
        "stock_name VARCHAR,fetched_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE ths_concept_daily("
        "trade_date DATE,concept_code VARCHAR,stock_count INTEGER)"
    )
    con.execute(
        "INSERT INTO ths_concept_stock_history VALUES "
        "('2026-07-28','301558','000001','old','2026-07-28 10:00:00'),"
        "('2026-07-28','301558','000001.SZ','new','2026-07-28 11:00:00'),"
        "('2026-07-28','301558','830799.NQ','other','2026-07-28 11:00:00')"
    )
    con.execute(
        "INSERT INTO ths_concept_daily VALUES ('2026-07-28','301558',3)"
    )
    con.close()

    result = repair_critical_integrity(db_path)

    assert result["ths_member_codes"]["suffixed_rows"] == 2
    assert result["ths_member_codes"]["removed_rows"] == 1
    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT stock_code,stock_name FROM ths_concept_stock_history "
            "ORDER BY stock_code"
        ).fetchall()
        stock_count = con.execute(
            "SELECT stock_count FROM ths_concept_daily"
        ).fetchone()[0]
        archived = con.execute(
            "SELECT stock_code FROM _dedupe_archive_ths_concept_stock_history"
        ).fetchall()
        unique_index = con.execute(
            "SELECT count(*) FROM duckdb_indexes() "
            "WHERE index_name='uq_ths_concept_member_business'"
        ).fetchone()[0]
    finally:
        con.close()
    assert rows == [("000001", "new"), ("830799", "other")]
    assert stock_count == 2
    assert archived == [("000001",)]
    assert unique_index == 1


def test_integrity_ths_normalization_dry_run_does_not_write(tmp_path):
    db_path = tmp_path / "ths-dry.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE ths_concept_stock_history("
        "trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,"
        "fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO ths_concept_stock_history VALUES "
        "('2026-07-28','301558','000001.SZ','2026-07-28 11:00:00')"
    )
    con.close()

    result = repair_critical_integrity(db_path, dry_run=True)

    assert result["ths_member_codes"]["status"] == "dry_run"
    assert result["ths_member_codes"]["suffixed_rows"] == 1
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert con.execute(
            "SELECT stock_code FROM ths_concept_stock_history"
        ).fetchone()[0] == "000001.SZ"
    finally:
        con.close()
