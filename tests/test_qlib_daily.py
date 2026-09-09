import duckdb

from scripts.run_qlib_daily import run
from scripts.run_qlib_research_daily import _prediction_refresh_dates


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


def test_qlib_research_refresh_dates_catch_up_missing_sessions(tmp_path):
    db = tmp_path / "research-gap.duckdb"
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE qlib_model_registry("
            "model_id VARCHAR, status VARCHAR, train_end VARCHAR)"
        )
        con.execute(
            "INSERT INTO qlib_model_registry VALUES ('shadow-v1','shadow','2026-08-13')"
        )
        con.execute(
            "CREATE TABLE qlib_prediction("
            "trade_date VARCHAR, symbol VARCHAR, model_id VARCHAR)"
        )
        con.execute(
            "INSERT INTO qlib_prediction VALUES ('2026-08-13','000001','shadow-v1')"
        )
        con.execute(
            "CREATE TABLE tushare_daily("
            "date DATE, stock_code VARCHAR, close DOUBLE)"
        )
        for trade_date in ("2026-08-14", "2026-08-17", "2026-08-18", "2026-08-19"):
            con.execute(
                "INSERT INTO tushare_daily VALUES (?, '000001', 10.0)",
                [trade_date],
            )
    finally:
        con.close()

    dates, previous = _prediction_refresh_dates(db, "2026-08-19")

    assert previous == "2026-08-13"
    assert dates == ["2026-08-14", "2026-08-17", "2026-08-18", "2026-08-19"]
