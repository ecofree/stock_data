import duckdb

from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables, import_qlib_predictions
from trade_system.ml.shadow_evaluator import evaluate_qlib_shadow


def test_import_qlib_predictions_persists_model_and_scores(tmp_path):
    db_path = tmp_path / "qlib.duckdb"
    rows = [
        {"trade_date": "2026-07-06", "symbol": "000001", "score": 0.91, "rank": 1, "horizon": "t1"},
        {"trade_date": "2026-07-06", "symbol": "000002", "score": 0.12, "rank": 2, "horizon": "t1"},
    ]

    result = import_qlib_predictions(
        db_path,
        model_id="shadow.alstm",
        model_name="ALSTM shadow",
        factor_set="Alpha158",
        rows=rows,
    )

    assert result["qlib_model_registry"] == 1
    assert result["qlib_prediction"] == 2
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM qlib_model_registry").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM qlib_prediction").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM v_qlib_shadow_candidate_overlap").fetchone()[0] == 2
    finally:
        con.close()


def test_evaluate_qlib_shadow_uses_next_close_without_affecting_signals(tmp_path):
    db_path = tmp_path / "qlib_eval.duckdb"
    ensure_qlib_shadow_tables(db_path)
    import_qlib_predictions(
        db_path,
        model_id="shadow.alstm",
        model_name="ALSTM shadow",
        factor_set="Alpha158",
        rows=[
            {"trade_date": "2026-07-06", "symbol": "000001", "score": 0.91, "rank": 1, "horizon": "t1"},
            {"trade_date": "2026-07-06", "symbol": "000002", "score": 0.12, "rank": 2, "horizon": "t1"},
        ],
    )
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR, close DOUBLE, ktype VARCHAR)")
        con.execute(
            """
            INSERT INTO kline VALUES
              ('2026-07-06','000001',10.0,'D'), ('2026-07-07','000001',11.0,'D'),
              ('2026-07-06','000002',10.0,'D'), ('2026-07-07','000002',9.0,'D')
            """
        )
    finally:
        con.close()

    result = evaluate_qlib_shadow(db_path)

    assert result["sample_count"] == 2
    assert result["models"]["shadow.alstm"]["hit_rate"] == 50.0
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM qlib_shadow_evaluation").fetchone()[0] == 1
    finally:
        con.close()
