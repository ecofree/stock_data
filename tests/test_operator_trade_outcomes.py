
import hashlib

import duckdb
import pytest

from trade_system.operator_backtest import run_operator_stage_backtest
from trade_system.operator_outcomes import import_operator_trade_outcomes
from trade_system.operator_outcomes import init_trading_tables


def test_retired_import_cannot_touch_existing_or_missing_database(tmp_path, monkeypatch):
    for key in ('STOCKDATA_ALLOW_LEGACY_WRITE','ALLOW_LEGACY_OPERATOR_IMPORT'):
        monkeypatch.setenv(key,'1')
    db = tmp_path/'existing.duckdb'
    init_trading_tables(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='RETIRED'):
        import_operator_trade_outcomes(db,tmp_path/'missing.csv')
    assert hashlib.sha256(db.read_bytes()).hexdigest()==before
    missing = tmp_path/'absent.duckdb'
    with pytest.raises(ValueError,match='RETIRED'):
        import_operator_trade_outcomes(missing,tmp_path/'missing.csv')
    assert not missing.exists()


def test_historical_executed_outcome_remains_readable(tmp_path):
    db = tmp_path/'history.duckdb'
    init_trading_tables(db)
    with duckdb.connect(str(db)) as con:
        con.execute("""INSERT INTO operator_trade_outcome(trade_date,stock_code,stock_name,
            execution_status,entry_price,exit_price,gross_return_pct,net_return_pct)
            VALUES ('2026-07-06','000001','Historical fixture','executed',10,10.8,8,7.8)""")
    result = run_operator_stage_backtest(db)
    assert result['sample_count']==1
    assert result['rows'][0]['source']=='real_outcome'
    assert result['rows'][0]['execution_status']=='executed'


def test_operator_backtest_handles_empty_outcome_table_without_signal_table(tmp_path):
    db_path = tmp_path / "empty_outcome.duckdb"
    init_trading_tables(db_path)

    result = run_operator_stage_backtest(db_path)

    assert result["sample_count"] == 0
    assert result["rows"] == []


def test_unfilled_operator_outcome_is_not_counted_as_return_sample(tmp_path):
    db_path = tmp_path / "unfilled.duckdb"
    init_trading_tables(db_path)
    with duckdb.connect(str(db_path)) as con:
        con.execute("""INSERT INTO operator_trade_outcome(trade_date,stock_code,stock_name,execution_status,position_pct)
            VALUES ('2026-07-06','000001','Historical fixture','skipped',0)""")
    result = run_operator_stage_backtest(db_path)

    assert result["mode"] == "real_outcome_incomplete"
    assert result["sample_count"] == 0
    assert result["excluded_unfilled_count"] == 1
    assert result["readiness"]["ready"] is False
