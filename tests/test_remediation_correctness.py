"""Independent counterexamples from the V2 review; no live database access."""
from dataclasses import replace
from itertools import product
import csv
import json
import sys

import duckdb
import pytest

from trade_system.backtest_engine import BacktestParams, simulate
from trade_system.gate_contract import build_operator_state
from trade_system.operator_risk import OperatorRiskConfig, TradePlanInput, evaluate_trade_plan


def test_execution_permission_requires_every_gate_and_no_blockers():
    for source, pipeline, artifact, data, flow, requested, blocked in product((False, True), repeat=7):
        state = build_operator_state(
            source_ready=source, pipeline_ready=pipeline, artifact_current=artifact,
            data_certified_ready=data, flow_certified_ready=flow,
            execution_ready=requested, blockers=['hard_block'] if blocked else [],
        )
        expected = all((source, pipeline, artifact, data, flow, requested)) and not blocked
        assert state['execution_ready'] is expected
        assert (state['operator_status'] == 'executable') is expected


@pytest.mark.parametrize('field', ['score', 'planned_position_pct', 'current_total_position_pct'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), None, True, '5'])
def test_invalid_risk_input_never_grants_permission(field, value):
    plan = TradePlanInput('TEST', 90, 5, 0, 'normal')
    assert not evaluate_trade_plan(replace(plan, **{field: value})).allowed


@pytest.mark.parametrize('field', list(OperatorRiskConfig.__dataclass_fields__))
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, None])
def test_invalid_risk_policy_never_grants_permission(field, value):
    cfg = replace(OperatorRiskConfig(), **{field: value})
    assert not evaluate_trade_plan(TradePlanInput('TEST', 90, 5, 0, 'normal'), cfg).allowed


def scenario(closes=(12, 12), entry_close=10):
    sessions = ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04']
    universe = [{'date': sessions[0], 'stock_code': code, 'board': 1} for code in ['A', 'B']]
    bars = {}
    for code, final in zip(['A', 'B'], closes):
        for day, close in zip(sessions, [10, entry_close, final, final]):
            bars[(day, code)] = {'open': 10, 'high': max(10, close), 'low': min(10, close), 'close': close}
    return universe, bars, sessions


def test_parallel_positions_preserve_cash_and_quantity():
    result = simulate(*scenario(), BacktestParams(capital=100, max_positions=2, slippage_bps=0))
    assert result['equity_curve'][-1] == pytest.approx(120)
    assert result['stats']['cumulative_return_pct'] == pytest.approx(20)
    assert all(row['cash'] >= 0 for row in result['daily_ledger'])
    assert sum(t.quantity * t.entry_price for t in result['trades']) == pytest.approx(100)


def test_open_position_drawdown_is_recorded_before_profitable_exit():
    result = simulate(*scenario(entry_close=5), BacktestParams(capital=100, max_positions=2, slippage_bps=0))
    assert result['stats']['max_drawdown_pct'] == pytest.approx(50)
    assert result['equity_curve'][-1] == pytest.approx(120)


def test_even_sample_median_averages_the_two_middle_values():
    result = simulate(*scenario(closes=(11, 13)), BacktestParams(capital=100, max_positions=2, slippage_bps=0))
    assert result['stats']['median_ret_pct'] == pytest.approx(20)


@pytest.mark.parametrize('field,value', [('hold_days', 0), ('hold_days', -1), ('hold_days', 1.5),
                                      ('max_positions', 0), ('capital', float('nan')),
                                      ('slippage_bps', -1), ('slippage_bps', 10000)])
def test_backtest_rejects_invalid_parameters(field, value):
    with pytest.raises(ValueError):
        simulate(*scenario(), replace(BacktestParams(), **{field: value}))


def test_missing_exit_does_not_delete_entry_and_can_recover_next_session():
    universe, bars, sessions = scenario()
    del bars[(sessions[2], 'A')]
    result = simulate(universe, bars, sessions, BacktestParams(capital=100, max_positions=2, slippage_bps=0))
    assert len(result['trades']) == 2
    assert next(t for t in result['trades'] if t.stock_code == 'A').exit_date == sessions[3]
    assert result['daily_ledger'][2]['stale_marks'] == ['A']


def test_terminal_unpriced_position_remains_in_ledger():
    universe, bars, sessions = scenario()
    for day in sessions[2:]:
        del bars[(day, 'A')]
    result = simulate(universe, bars, sessions, BacktestParams(capital=100, max_positions=2, slippage_bps=0))
    assert result['stats']['open_positions'] == 1
    assert result['open_positions'][0]['stock_code'] == 'A'
    assert result['daily_ledger'][-1]['market_value'] == pytest.approx(50)
    assert result['stats']['valuation_complete'] is False


def test_duplicate_candidates_do_not_double_spend_and_tail_entries_are_kept():
    universe, bars, sessions = scenario()
    universe = [universe[0], universe[0], {'date': sessions[-2], 'stock_code': 'B', 'board': 1}]
    result = simulate(universe, bars, sessions, BacktestParams(capital=100, max_positions=2, slippage_bps=0))
    assert len(result['intents']) == 2
    assert len(result['trades']) == 1
    assert result['open_positions'][0]['entry_date'] == sessions[-1]
    for day in result['daily_ledger']:
        assert day['cash'] >= 0
        assert day['equity'] == pytest.approx(day['cash'] + day['market_value'])
        assert day['market_value'] == pytest.approx(sum(p['quantity'] * p['mark'] for p in day['positions']))


def test_future_exit_prices_do_not_change_entry_budget():
    params = BacktestParams(capital=100, max_positions=2, slippage_bps=0)
    original = simulate(*scenario(), params)
    changed = simulate(*scenario(closes=(100, 1)), params)
    assert original['intents'] == changed['intents']
    assert original['daily_ledger'][:2] == changed['daily_ledger'][:2]


def test_cli_exports_trades_and_daily_ledger_using_an_isolated_database(tmp_path, monkeypatch):
    from scripts import run_strategy_backtest as cli

    db = tmp_path / 'fixture.duckdb'
    universe, bars, sessions = scenario()
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE v_limit_pool(trade_date VARCHAR, stock_code VARCHAR, board_level INTEGER)')
        con.executemany('INSERT INTO v_limit_pool VALUES (?, ?, ?)',
                        [(r['date'], r['stock_code'], r['board']) for r in universe])
        con.execute('CREATE TABLE v_kline_daily(trade_date VARCHAR, stock_code VARCHAR, '
                    'open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, ktype VARCHAR, '
                    'is_fallback BOOLEAN, fetched_at TIMESTAMP)')
        con.executemany("INSERT INTO v_kline_daily VALUES (?,?,?,?,?,?,'D',false,now())",
                        [(day, code, b['open'], b['high'], b['low'], b['close'])
                         for (day, code), b in bars.items()])
    prefix = tmp_path / 'backtest'
    monkeypatch.setattr(cli, 'configure', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['run_strategy_backtest', '--db', str(db),
                        '--start', sessions[0], '--end', sessions[-1], '--slippage-bps', '0',
                        '--max-positions', '2', '--out-prefix', str(prefix)])
    assert cli.main() == 0
    with (tmp_path / 'backtest_trades_latest.csv').open(encoding='utf-8', newline='') as fh:
        trades = list(csv.DictReader(fh))
    assert len(trades) == 2
    assert sum(float(t['pnl']) for t in trades) == pytest.approx(200000)
    assert sum(float(t['quantity']) * float(t['entry_price']) for t in trades) == pytest.approx(1000000)
    ledger = json.loads((tmp_path / 'backtest_ledger_latest.json').read_text(encoding='utf-8'))
    assert [r['date'] for r in ledger['daily_ledger']] == sessions
    assert ledger['daily_ledger'][-1]['equity'] == pytest.approx(1200000)
    assert ledger['scope'] == 'research_only_daily_bar_proxy'
    assert 'research_only_daily_bar_proxy' in (tmp_path / 'backtest_latest.md').read_text(encoding='utf-8')
