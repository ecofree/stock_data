import duckdb
import pytest

from trade_system.operator_backtest import run_operator_stage_backtest
from trade_system.v2.domain import file_hash


@pytest.mark.parametrize('period', ['D', 'd'])
def test_historical_signals_never_stand_in_for_real_executions(tmp_path, period):
    db = tmp_path / 'manual.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE stock_candidate_stage_signal(trade_date DATE,stock_code VARCHAR,score DOUBLE,is_actionable BOOLEAN)')
        con.execute("INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-06','000001',99,true)")
        con.execute('CREATE TABLE kline(date DATE,stock_code VARCHAR,open DOUBLE,close DOUBLE,ktype VARCHAR)')
        con.executemany("INSERT INTO kline VALUES (?,'000001',10,20,?)", [(d,period) for d in ['2026-07-06','2026-07-07','2026-07-08']])
        con.execute("CREATE TABLE watchlist(note VARCHAR); INSERT INTO watchlist VALUES ('human judgement')")
    before = file_hash(db)
    result = run_operator_stage_backtest(db)
    assert result['mode'] == 'no_real_outcomes'
    assert result['sample_count'] == 0 and result['rows'] == []
    assert result['summary']['real_outcome_count'] == result['summary']['proxy_signal_count'] == 0
    assert result['readiness']['ready'] is False
    assert file_hash(db) == before
