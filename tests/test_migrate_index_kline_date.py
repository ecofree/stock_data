from __future__ import annotations

import duckdb

from scripts.migrate_index_kline_date import migrate


def test_migrate_index_kline_date_is_idempotent(tmp_path):
    db = tmp_path / "index-date.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE index_kline(date VARCHAR, index_code VARCHAR, ktype VARCHAR)")
    con.execute("INSERT INTO index_kline VALUES ('2026-07-16','000001','D')")
    con.close()

    assert migrate(db)["status"] == "migrated"
    assert migrate(db)["status"] == "already_date"
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT data_type FROM information_schema.columns WHERE table_name='index_kline' AND column_name='date'").fetchone()[0] == "DATE"
    assert con.execute("SELECT date FROM index_kline").fetchone()[0].isoformat() == "2026-07-16"
    con.close()


def test_migrate_index_kline_date_refuses_invalid_values(tmp_path):
    db = tmp_path / "invalid-index-date.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE index_kline(date VARCHAR, index_code VARCHAR, ktype VARCHAR)")
    con.execute("INSERT INTO index_kline VALUES ('not-a-date','000001','D')")
    con.close()

    try:
        migrate(db)
    except ValueError as exc:
        assert "invalid date" in str(exc)
    else:
        raise AssertionError("migration should refuse invalid date values")
