import json
from pathlib import Path

import duckdb
import pytest

from trade_system.v2.domain import canonical
from trade_system.v2.paper_ledger import PaperBook
from trade_system.v2.portfolio_diagnostic import evaluate_portfolio, fee, validate_policy
from trade_system.v2 import portfolio_experiment as exp


@pytest.fixture
def policy():
    return json.loads((Path(__file__).resolve().parents[1]/'docs/v2/portfolio_policy_20260910.json').read_text())


@pytest.fixture
def sample():
    days = ['2026-04-01', '2026-04-02', '2026-04-03', '2026-04-07', '2026-04-08']
    return {'predictions': [[days[0], '000001', 2.0]],
            'bars': [{'date': day, 'instrument': 'SZ.000001', 'open_fen': 1000, 'close_fen': 1100, 'factor': '1'} for day in days],
            'calendar': days, 'identity_map': {'000001': 'SZ.000001'}}


def test_integer_cash_and_no_label_compounding(policy, sample):
    r = evaluate_portfolio(**sample, policy=policy)
    assert r['period_complete'] and r['portfolio_return'] is None and not r['execution_ready']
    buy, sell = r['fills']
    assert buy['date'] == '2026-04-02' and sell['date'] == '2026-04-03'
    assert buy['quantity'] == sell['quantity'] == 400  # fee budget excludes 500 shares
    assert r['cash_fen'] == policy['initial_cash_fen'] + 400*100 - buy['fee_fen'] - sell['fee_fen']
    assert all(n['reconciliation_residual_fen'] == 0 for n in r['nav'])
    assert r['actual_operator_return'] is None


@pytest.mark.parametrize('key,value', [('hold_sessions', 1), ('t_plus_sessions', 0), ('top_k', True),
    ('buy_lot', 0), ('ticket_bps', 10001), ('initial_cash_fen', 0), ('scope', 'live'),
    ('fill_assumption', 'observed_orderbook'), ('calendar_assumption', 'inferred'),
    ('missing_mark_policy', 'zero'), ('corporate_action_policy', 'ignore')])
def test_invalid_contract(policy, key, value):
    policy[key] = value
    with pytest.raises(ValueError):
        validate_policy(policy)


@pytest.mark.parametrize('reason', ['missing_entry_bar', 'missing_entry_factor', 'insufficient_ticket_or_cash'])
def test_missed_entry_never_shifted_to_next_day(policy, sample, reason):
    if reason == 'missing_entry_bar':
        sample['bars'] = [r for r in sample['bars'] if r['date'] != '2026-04-02']
    elif reason == 'missing_entry_factor':
        sample['bars'][1]['factor'] = None
    else:
        policy['ticket_bps'] = 1
    r = evaluate_portfolio(**sample, policy=policy)
    assert r['fills'] == [] and r['skip_counts'] == {reason: 1}
    assert r['hypothetical_return'] == '0'


@pytest.mark.parametrize('reason', ['missing_held_factor', 'unresolved_corporate_action', 'missing_current_mark'])
def test_invalid_held_account_retained_not_liquidated(policy, sample, reason):
    row = sample['bars'][2]
    if reason == 'missing_held_factor':
        row['factor'] = None
    elif reason == 'unresolved_corporate_action':
        row['factor'] = '2'
    else:
        row['close_fen'] = None
    r = evaluate_portfolio(**sample, policy=policy)
    assert not r['period_complete'] and r['blocker']['reason'] == reason
    assert len(r['fills']) == 1 and r['open_positions']['SZ.000001']['quantity'] == 400
    assert r['hypothetical_return'] is None and r['hypothetical_max_daily_drawdown'] is None
    assert r['last_fully_valued']['date'] == '2026-04-02'


def test_overlap_and_cash_do_not_use_today_close_at_open(policy, sample):
    sample['identity_map']['600001'] = 'SH.600001'
    sample['bars'] += [{**r, 'instrument': 'SH.600001'} for r in sample['bars']]
    sample['predictions'] += [['2026-04-02', '600001', 3.0], ['2026-04-02', '000001', 2.0]]
    policy.update(initial_cash_fen=150000, ticket_bps=10000)
    r = evaluate_portfolio(**sample, policy=policy)
    assert r['skip_counts'] == {'insufficient_ticket_or_cash': 1, 'already_held': 1}
    assert len(r['fills']) == 2 and min(n['cash_fen'] for n in r['nav']) >= 0


def test_top_k_ties_and_caps_no_lower_rank_replacement(policy, sample):
    sample['identity_map']['600001'] = 'SH.600001'
    sample['bars'] += [{**r, 'instrument': 'SH.600001'} for r in sample['bars']]
    sample['predictions'] += [['2026-04-01', '600001', 2.0]]
    policy['max_positions'] = 1
    r = evaluate_portfolio(**sample, policy=policy)
    assert r['fills'][0]['instrument'] == 'SZ.000001' and r['skip_counts'] == {'position_cap': 1}
    policy['top_k'] = 1
    sample['bars'][1]['open_fen'] = None
    r = evaluate_portfolio(**sample, policy=policy)
    assert not r['fills'] and r['skip_counts'] == {'missing_entry_bar': 1}


def test_missing_calendar_duplicate_and_nonfinite(policy, sample):
    with pytest.raises(ValueError, match='horizon'):
        evaluate_portfolio(**{**sample, 'calendar': sample['calendar'][:2], 'bars': sample['bars'][:2]}, policy=policy)
    for rows in [sample['predictions']*2, [['2026-04-01', '000001', float('nan')]], [['2026-04-01', '123456', 1]]]:
        with pytest.raises(ValueError):
            evaluate_portfolio(**{**sample, 'predictions': rows}, policy=policy)
    with pytest.raises(ValueError, match='duplicate bar'):
        evaluate_portfolio(**{**sample, 'bars': sample['bars']*2}, policy=policy)


def test_tplus_and_drawdown_across_holding(policy, sample):
    policy.update(hold_sessions=3, t_plus_sessions=2)
    sample['bars'][2]['close_fen'] = 500
    r = evaluate_portfolio(**sample, policy=policy)
    assert r['fills'][1]['date'] == '2026-04-07'
    assert float(r['hypothetical_max_daily_drawdown']) > .02


def test_synthetic_paper_ledger_parity_does_not_weaken_book_guard(policy, sample):
    """Synthetic order books ONLY. Real daily bars must still fail the book guard."""
    r = evaluate_portfolio(**sample, policy=policy)
    rule = {'version': 'fixture', 'effective_from': sample['calendar'][0], 'effective_to': sample['calendar'][-1],
            'buy_lot': 100, 'sell_lot': 100, 't_plus_sessions': 1, 'allow_odd_sell_all': True}
    config = {'account_id': 'fixture-not-real', 'mode': 'paper', 'trading_days': sample['calendar'],
        'opened_at': '2026-04-01T09:00:00+08:00', 'initial_cash_fen': policy['initial_cash_fen'],
        'initial_lots': [], 'quote_ttl_seconds': 60, 'mark_ttl_seconds': 60, 'account_ttl_seconds': 60,
        'fees': {**policy['fees'], 'effective_from': rule['effective_from'], 'effective_to': rule['effective_to']},
        'instruments': {'SZ.000001': rule}}
    book = PaperBook(config)
    for i, f in enumerate(r['fills']):
        oid = f'fixture-{i}'
        book.apply({'event_id': oid, 'kind': 'submit', 'at': f["date"]+'T14:00:00+08:00',
            'payload': {'order_id': oid, 'instrument': f['instrument'], 'side': f['side'],
                        'quantity': f['quantity'], 'limit_price_fen': f['price_fen'], 'decision_ref': 'synthetic-only'}})
        quote = {'instrument': f['instrument'], 'source_event_at': f['date']+'T14:00:01+08:00',
                 'evidence_kind': 'daily_bar', 'phase': 'continuous', 'tradable': True,
                 'rule_version': 'fixture', 'lower_limit_fen': 1, 'upper_limit_fen': 10000,
                 'bid_fen': f['price_fen'], 'ask_fen': f['price_fen'], 'last_fen': f['price_fen'],
                 'bid_quantity': f['quantity'], 'ask_quantity': f['quantity']}
        before = len(book.state['fills'])
        book.apply({'event_id': oid+'-bar', 'kind': 'market', 'at': quote['source_event_at'], 'payload': quote})
        assert len(book.state['fills']) == before
        synthetic = {**quote, 'evidence_kind': 'observed_orderbook', 'source_event_at': f['date']+'T14:00:02+08:00'}
        book.apply({'event_id': oid+'-synthetic', 'kind': 'market', 'at': synthetic['source_event_at'], 'payload': synthetic})
    assert book.summary()['cash_fen'] == r['cash_fen']
    assert book.summary()['fees_fen'] == r['fees_fen']
    assert book.summary()['realized_pnl_fen'] == r['realized_pnl_fen']
    for side in ('buy', 'sell'):
        for notional in (0, 1, 12345, 5000000):
            assert fee(policy, notional, side) == book.fee(notional, side)


@pytest.fixture
def frozen(tmp_path, monkeypatch, policy):
    parent = tmp_path/'parent'
    fold = parent/'run/fold-00'
    fold.mkdir(parents=True)
    (parent/'run/completed.json').write_text('{}')
    (parent/'registration.json').write_text(canonical({'plan': {'variants': {'price': [], 'funds': []}}}))
    (fold/'test_ids.json').write_text(canonical([['2026-04-01', '000001']]))
    for name in ('price', 'funds'):
        (fold/name).mkdir()
        (fold/name/'predictions.csv').write_text('datetime,instrument,prediction\n2026-04-01,000001,2\n')
    monkeypatch.setattr(exp, 'read_result', lambda _: {'registration_id': 'fixture-parent'})
    db = tmp_path/'snapshot.duckdb'
    with duckdb.connect(str(db)) as c:
        c.execute("CREATE TABLE tushare_trade_cal AS SELECT 'SSE' exchange, d::DATE cal_date,true is_open,(d-INTERVAL 1 DAY)::DATE pretrade_date FROM range(DATE '2026-04-01',DATE '2026-04-05',INTERVAL 1 DAY) r(d)")
        c.execute("CREATE TABLE tushare_daily AS SELECT '000001' stock_code,'000001.SZ' ts_code,cal_date AS date,10.0 AS open,11.0 AS close,'none' adjustment,TIMESTAMP '2026-09-09 18:00:00' fetched_at,'fixture' provider FROM tushare_trade_cal")
        c.execute('CREATE TABLE tushare_adj_factor AS SELECT stock_code,ts_code,date,1.0 adj_factor,fetched_at FROM tushare_daily')
    return parent, db, policy, tmp_path/'portfolio'


def test_frozen_roundtrip_cash_baseline_and_immutable_outputs(frozen):
    record = exp.freeze_inputs(*frozen)
    assert record['available_bars'] == 2 and record['requested_bar_identities'] == 2
    r = exp.run_portfolio(frozen[3])
    assert r['common_full_period_comparison_available'] and r['cash_control']['return'] == '0'
    assert r == exp.read_portfolio_result(frozen[3])
    assert len(r['accounts']) == 3
    with pytest.raises(FileExistsError):
        exp.run_portfolio(frozen[3])
    with pytest.raises(FileExistsError):
        exp.freeze_inputs(*frozen)
    (frozen[3]/'run/results.json').write_text('{}')
    with pytest.raises(ValueError, match='outputs changed'):
        exp.read_portfolio_result(frozen[3])


@pytest.mark.parametrize('kind', ['inputs', 'policy', 'code', 'parent'])
def test_freeze_tampering(frozen, monkeypatch, kind):
    exp.freeze_inputs(*frozen)
    folder = frozen[3]
    if kind == 'inputs':
        (folder/'inputs.json').write_text('{}')
    elif kind == 'policy':
        record = json.loads((folder/'registration.json').read_text())
        record['policy']['top_k'] = 1
        (folder/'registration.json').write_text(canonical(record))
    elif kind == 'parent':
        (frozen[0]/'run/completed.json').write_text('{"changed":true}')
    else:
        monkeypatch.setattr(exp, 'source_hashes', lambda: {})
    with pytest.raises(ValueError):
        exp.run_portfolio(folder)
    assert not (folder/'run').exists()


@pytest.mark.parametrize('sql', ["UPDATE tushare_daily SET adjustment='qfq'",
    'INSERT INTO tushare_daily SELECT * FROM tushare_daily',
    "UPDATE tushare_daily SET ts_code='000001.SH' WHERE date='2026-04-02'",
    "DELETE FROM tushare_trade_cal WHERE cal_date='2026-04-02'",
    "UPDATE tushare_trade_cal SET pretrade_date='2026-03-01' WHERE cal_date='2026-04-03'"])
def test_source_ambiguity_rejected(frozen, sql):
    with duckdb.connect(str(frozen[1])) as c:
        c.execute(sql)
    with pytest.raises(ValueError):
        exp.freeze_inputs(*frozen)


def test_one_blocked_variant_no_unequal_window_comparison(frozen, monkeypatch):
    exp.freeze_inputs(*frozen)
    original = exp.evaluate_portfolio
    calls = []
    def evaluate(*args):
        result = original(*args)
        if not calls:
            result.update(period_complete=False, hypothetical_return=None)
        calls.append(1)
        return result
    monkeypatch.setattr(exp, 'evaluate_portfolio', evaluate)
    result = exp.run_portfolio(frozen[3])
    assert not result['common_full_period_comparison_available']
    assert result['hypothetical_excess_over_cash'] is None


def test_failed_run_cannot_be_reused(frozen, monkeypatch):
    exp.freeze_inputs(*frozen)
    def fail(*args):
        raise RuntimeError('fixture failure')
    monkeypatch.setattr(exp, 'evaluate_portfolio', fail)
    with pytest.raises(RuntimeError):
        exp.run_portfolio(frozen[3])
    assert (frozen[3]/'run/failed.json').exists()
    assert not (frozen[3]/'run/completed.json').exists()
    with pytest.raises(FileExistsError):
        exp.run_portfolio(frozen[3])


def test_calendar_independent_source_links_not_weekday_guessing(frozen):
    with duckdb.connect(str(frozen[1])) as c:
        c.execute("UPDATE tushare_trade_cal SET pretrade_date=NULL WHERE cal_date='2026-04-02'")
    record = exp.freeze_inputs(*frozen)
    assert record['calendar_quality']['null_pretrade_linked_by_complete_civil_rows'] == ['2026-04-02']


def test_calendar_cannot_bridge_both_missing_sources(frozen):
    with duckdb.connect(str(frozen[1])) as c:
        c.execute("DELETE FROM tushare_trade_cal WHERE cal_date='2026-04-02'")
        c.execute("UPDATE tushare_trade_cal SET pretrade_date=NULL WHERE cal_date='2026-04-03'")
    with pytest.raises(ValueError, match='neither'):
        exp.freeze_inputs(*frozen)
