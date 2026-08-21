import duckdb

from scripts.run_qlib_daily import run


def test_qlib_daily_is_fail_closed_without_champion(tmp_path):
    db = tmp_path / "daily.duckdb"
    out = tmp_path / "qlib_daily.json"
    result = run(db, feature_file=tmp_path / "missing.parquet", out=out)
    assert result["status"] == "no_champion"
    con = duckdb.connect(str(db), read_only=True)
    try:
        names = {row[0] for row in con.execute("select table_name from information_schema.tables").fetchall()}
        assert {"paper_order", "paper_position"}.issubset(names)
    finally:
        con.close()

