import hashlib

import duckdb
import pytest

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


def test_evaluate_qlib_shadow_uses_calendar_without_database_writes(tmp_path):
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

    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)")
        con.execute("INSERT INTO tushare_trade_cal VALUES ('2026-07-06',true),('2026-07-07',true)")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = evaluate_qlib_shadow(db_path)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert result['database_writes'] == 0 and not result['execution_ready']

    assert result["sample_count"] == 2
    assert result["models"]["shadow.alstm"]["hit_rate"] == 50.0
    assert result["models"]["shadow.alstm"]["ic"] == 1.0
    assert result["models"]["shadow.alstm"]["rank_ic"] == 1.0
    assert result["models"]["shadow.alstm"]["top_bottom_spread"] == 20.0
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM qlib_shadow_evaluation").fetchone()[0] == 0
    finally:
        con.close()


def test_shared_rank_ic_keeps_ties_constants_and_small_samples():
    from trade_system.review_metrics import average_ranks, correlation
    from trade_system.ml import shadow_evaluator
    assert shadow_evaluator.average_ranks is average_ranks
    assert shadow_evaluator.correlation is correlation
    assert average_ranks([20, 10, 20, 30]) == [2.5, 1.0, 2.5, 4.0]
    assert correlation([1, 1], [1, 2]) is None
    assert correlation([1], [2]) is None
    assert correlation([1, 2], [1]) is None
    assert correlation([1, 2, 3], [3, 2, 1]) == -1.0


@pytest.mark.parametrize('problem', ['missing_entry', 'missing_exit', 'duplicate_entry', 'nan_price', 'bad_horizon'])
def test_shadow_labels_never_skip_required_sessions(tmp_path, problem):
    db = tmp_path/'shadow.duckdb'
    import_qlib_predictions(db, model_id='fixture', model_name='synthetic', factor_set='fixture',
        rows=[{'trade_date':'2026-07-06','symbol':'000001','score':1,'rank':1,
               'horizon':'unknown' if problem=='bad_horizon' else 't1_exec'}])
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)')
        con.execute("INSERT INTO tushare_trade_cal VALUES ('2026-07-06',true),('2026-07-07',true),('2026-07-08',true),('2026-07-09',true)")
        con.execute('CREATE TABLE kline(date DATE, stock_code VARCHAR, open DOUBLE, close DOUBLE, ktype VARCHAR)')
        con.execute("INSERT INTO kline VALUES ('2026-07-06','000001',10,10,'D'),('2026-07-07','000001',11,12,'D'),('2026-07-08','000001',12,13,'D'),('2026-07-09','000001',100,110,'D')")
        if problem=='missing_entry':con.execute("DELETE FROM kline WHERE date='2026-07-07'")
        if problem=='missing_exit':con.execute("DELETE FROM kline WHERE date='2026-07-08'")
        if problem=='duplicate_entry':con.execute("INSERT INTO kline SELECT * FROM kline WHERE date='2026-07-07'")
        if problem=='nan_price':con.execute("UPDATE kline SET open='NaN' WHERE date='2026-07-07'")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = evaluate_qlib_shadow(db)
    assert result['sample_count'] == 0 and len(result['excluded']) == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_shadow_execution_horizon_respects_closed_days_and_fails_unknown_calendar(tmp_path):
    db = tmp_path/'shadow.duckdb'
    import_qlib_predictions(db, model_id='fixture', model_name='synthetic', factor_set='fixture',
        rows=[{'trade_date':'2026-07-03','symbol':'000001','score':1,'rank':1,'horizon':'t1_exec'}])
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)')
        con.execute("INSERT INTO tushare_trade_cal VALUES ('2026-07-03',true),('2026-07-04',false),('2026-07-05',false),('2026-07-06',true),('2026-07-07',true)")
        con.execute('CREATE TABLE kline(date DATE, stock_code VARCHAR, open DOUBLE, close DOUBLE, ktype VARCHAR)')
        con.execute("INSERT INTO kline VALUES ('2026-07-06','000001',10,11,'D'),('2026-07-07','000001',11,12,'D')")
    result = evaluate_qlib_shadow(db)
    assert result['rows'][0]['entry_date'] == '2026-07-06'
    assert result['rows'][0]['forward_date'] == '2026-07-07'
    assert result['rows'][0]['forward_return_pct'] == 20
    with duckdb.connect(str(db)) as con:
        con.execute("DELETE FROM tushare_trade_cal WHERE cal_date='2026-07-05'")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='calendar'):
        evaluate_qlib_shadow(db)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
