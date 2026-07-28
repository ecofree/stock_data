import duckdb

from trade_system.integration.operator_views import build_operator_views


def test_operator_views_prefer_native_signals_and_include_legacy(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-06', 'premarket_pool', '000001', 'native stock', 91, 'watch', '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE legacy_qds_daily_watchlist("
        "date DATE, stock_code VARCHAR, stock_name VARCHAR, final_score DOUBLE, "
        "legacy_source_table VARCHAR, legacy_imported_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO legacy_qds_daily_watchlist VALUES "
        "('2026-07-05', '000002', 'legacy stock', 80, 'daily_watchlist', '2026-07-07 12:00:00')"
    )
    con.close()

    build_operator_views(db_path)

    con = duckdb.connect(str(db_path))
    rows = con.execute(
        "SELECT trade_date, stage, stock_code, data_origin "
        "FROM v_operator_candidates ORDER BY trade_date, stock_code"
    ).fetchall()
    con.close()
    assert rows == [
        ("2026-07-05", "legacy_watchlist", "000002", "legacy_qds"),
        ("2026-07-06", "premarket_pool", "000001", "stock_data"),
    ]
