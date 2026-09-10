from copy import deepcopy

import pytest

from trade_system.v2.domain import identity
from trade_system.v2.integrated_account import IntegratedAccount,replay
from trade_system.v2.integrated_run import event,fill,golden_scenarios,market,run_scenarios,verify_scenarios


def rights():
    return golden_scenarios()['entitlement_parent']


def account_until(index):
    s = rights()
    a = IntegratedAccount(s['config'])
    for e in s['events'][:index]:
        a.apply(e)
    return a,s


def test_hand_computed_integrated_parent_gold():
    result = replay(**rights())
    final = result['final']
    # 1,000,000 + (70,000-10) - (70,000+10) + 720 + (30,000-5) - 45
    assert final['cash_fen'] == 1030650
    assert final['fresh_equity_fen'] == 1315650  # + (200+100+80)*750
    assert final['held_cost_fen'] == 310010  # 171429 + 70010 + 68571
    assert final['realized_trading_pnl_fen'] == -16  # -1439 + 1423
    assert final['diagnostic_marked_return'] == str(__import__('decimal').Decimal(15650)/1300000)
    aid = next(iter(final['rights']))
    assert final['rights'][aid]['gross_cash_fen'] == 750  # later buyer gets no extra dividend
    assert final['rights'][aid]['delivered_quantity'] == 120
    assert final['rights'][aid]['lots'][0]['quantity'] == 100  # original already sold, rights frozen
    assert final['lots']['new-after-record']['cost_fen'] == 70010
    assert final['share_receivable_cost_fen'] == 0 and final['share_receivable_quantity']['SZ.000001'] == 0
    for snapshot in result['snapshots']:
        assert snapshot['cash_reconciliation_residual_fen'] == snapshot['cost_reconciliation_residual_fen'] == 0
        assert not any(snapshot['share_reconciliation_residual'].values())
        assert snapshot['account_reconciliation_residual_fen'] in (0,None)
        assert not snapshot['execution_ready'] and not snapshot['can_resume_historical_portfolio']
        assert snapshot['portfolio_return'] is None and snapshot['actual_operator_return'] is None


def test_cost_transfer_and_pre_ex_mark_invalidated_before_new_quote():
    a,s = account_until(2)
    summary = a.summary()
    assert summary['held_cost_fen'] == 242858
    assert summary['share_receivable_cost_fen'] == 97143
    assert summary['fresh_equity_fen'] is None
    assert summary['marks'][0]['reason'] == 'pre_action_mark_invalidated'
    assert summary['cash_fen'] == 1000000 and summary['cash_receivable_fen'] == 750
    assert summary['tax_unknown'] and summary['available_cash_fen'] is None
    assert not a.apply(s['events'][2])['tax_unknown']
    assert a.summary()['available_cash_fen'] == 999925  # liability reserve before payment


def test_receivable_becomes_physical_only_once_with_no_nav_jump():
    a,s = account_until(8)  # cash paid, shares not delivered
    assert a.summary()['holdings']['SZ.000001'] == 300
    assert a.summary()['share_receivable_quantity']['SZ.000001'] == 120
    a.apply(s['events'][8])
    summary = a.summary()
    assert summary['holdings']['SZ.000001'] == 420
    assert summary['share_receivable_quantity']['SZ.000001'] == 0
    assert summary['held_cost_fen'] == 338582 and summary['share_receivable_cost_fen'] == 0
    before = deepcopy(summary)
    assert a.apply(s['events'][8]) == before
    with pytest.raises(ValueError):
        a.apply({**s['events'][8],'event_id':'duplicate-credit-new-envelope'})
    assert a.summary() == before


def test_suspension_expiry_resume_partial_exit_gold():
    result = replay(**golden_scenarios()['suspension_exit'])
    carry = result['snapshots'][1]
    assert carry['net_indicative_equity_fen'] == 200000
    assert carry['fresh_equity_fen'] is None and carry['diagnostic_marked_return'] is None
    assert carry['marks'][0]['basis'] == 'indicative_suspended_carry_not_tradable'
    assert carry['marks'][0]['age_sessions'] == 1
    expired = result['snapshots'][3]
    assert expired['marks'][0]['reason'] == 'suspended_mark_expired'
    assert expired['net_indicative_equity_fen'] is None
    assert expired['holdings']['SZ.000001'] == 100 and expired['held_cost_fen'] == 100000
    partial = result['snapshots'][5]
    assert partial['exit_intents']['wait']['remaining'] == 50
    assert partial['exit_intents']['wait']['status'] == 'partial'
    final = result['final']
    assert final['cash_fen'] == final['fresh_equity_fen'] == 179980
    assert final['realized_trading_pnl_fen'] == -20020
    assert final['exit_intents']['wait']['status'] == 'satisfied'
    assert len(final['fills']) == 2 and all(not f['actual_fill'] for f in final['fills'])


def test_delisted_asset_and_unfulfilled_exit_are_retained():
    s = golden_scenarios()['delisted_retention']
    a = IntegratedAccount(s['config'])
    for e in s['events']:
        a.apply(e)
    final = a.summary()
    assert final['cash_fen'] == 100000 and final['holdings']['SZ.000001'] == 100
    assert final['held_cost_fen'] == 100000 and final['fresh_equity_fen'] is None
    assert final['marks'][0]['reason'] == 'delisted_asset_retained_without_valuation'
    assert final['exit_intents']['wait']['remaining'] == 100 and not final['fills']
    before = a.summary()
    with pytest.raises(ValueError):
        a.apply(market('resurrection','2026-05-22T10:00:00+08:00'))
    assert a.summary() == before


@pytest.mark.parametrize('field,value',[('scope','live'),('policy','forward_fill_all'),
    ('max_stale_sessions',True),('max_stale_sessions',21),('quote_ttl_seconds',0)])
def test_explicit_bounded_synthetic_policy(field,value):
    config = rights()['config']
    config[field] = value
    with pytest.raises(ValueError):
        IntegratedAccount(config)


@pytest.mark.parametrize('change',['duplicate_action','calendar_gap','late_acquisition','duplicate_lot','unknown_restriction','zero_mark'])
def test_config_identity_and_calendar_gates(change):
    config = rights()['config']
    if change == 'duplicate_action':
        config['actions'].append(deepcopy(config['actions'][0]))
    elif change == 'calendar_gap':
        config['calendar'].remove('2026-05-21')
    elif change == 'late_acquisition':
        config['lots'][0]['acquired_at'] = '2026-05-19T10:00:00+08:00'
    elif change == 'duplicate_lot':
        config['lots'][1]['lot_id'] = 'a'
    elif change == 'unknown_restriction':
        config['lots'][0]['restricted'] = None
    else:
        config['initial_marks']['SZ.000001'] = 0
    with pytest.raises(ValueError):
        IntegratedAccount(config)


def test_register_uses_parent_state_and_strict_record_close():
    a,s = account_until(0)
    bad = deepcopy(s['events'][0])
    bad['effective_at'] = '2026-05-18T14:59:59+08:00'
    with pytest.raises(ValueError):
        a.apply(bad)
    a.apply(s['events'][0])
    snapshot = next(iter(a.books.values())).config['snapshot']
    assert snapshot['lots'][0]['cost_fen'] == 100001
    assert snapshot['lots'][1]['restricted'] is True
    with pytest.raises(ValueError):
        a.apply({**s['events'][0],'event_id':'second-registration'})


def test_terms_unknown_at_registration_rejected():
    s = rights()
    s['config']['actions'][0]['available_at'] = '2026-05-19T09:00:00+08:00'
    a = IntegratedAccount(s['config'])
    with pytest.raises(ValueError):
        a.apply(s['events'][0])


def test_fractional_accrual_rolls_back_parent_and_child():
    s = rights()
    s['config']['lots'][1]['quantity'] = 201
    a = IntegratedAccount(s['config'])
    a.apply(s['events'][0])
    before = a.summary()
    with pytest.raises(ValueError,match='fractional'):
        a.apply(s['events'][1])
    assert a.summary() == before
    assert next(iter(a.books.values())).state['phase'] == 'recorded'


def test_insufficient_parent_cash_rolls_back_child_tax_payment():
    s = rights()
    s['config']['initial_cash_fen'] = 0
    a = IntegratedAccount(s['config'])
    for e in s['events'][:2]:
        a.apply(e)
    aid = s['config']['actions'][0]['action_id']
    # Gross cash payment is not received, so no cash is fabricated to pay tax.
    a.apply(event('assessed','entitlement','2026-05-19T09:31:00+08:00',
        {'action_id':aid,'kind':'tax_assessment','value':{'total_fen':1000,'basis_ref':'fixture'}}))
    payment = deepcopy(s['events'][7])
    a.apply(payment)
    before = a.summary()
    with pytest.raises(ValueError,match='cash cannot fund'):
        a.apply(event('tax-debit','entitlement','2026-05-20T11:00:00+08:00',
            {'action_id':aid,'kind':'tax_payment','value':{'amount_fen':970}}))
    assert a.summary() == before
    assert next(iter(a.books.values())).state['tax_paid_later_fen'] == 0


def test_no_buy_from_unpaid_cash_rights_or_unknown_tax():
    s = rights()
    s['config']['initial_cash_fen'] = 0
    a = IntegratedAccount(s['config'])
    a.apply(s['events'][0]); a.apply(s['events'][1])
    a.apply(s['events'][3])
    before = a.summary()
    with pytest.raises(ValueError):
        a.apply(s['events'][6])
    assert a.summary() == before


@pytest.mark.parametrize('status',['suspended','unknown','delisted'])
def test_no_fill_when_no_trading_session(status):
    s = golden_scenarios()['suspension_exit']
    a = IntegratedAccount(s['config'])
    a.apply(market('status','2026-05-19T10:00:00+08:00',status=status))
    a.apply(event('exit','exit_intent','2026-05-19T10:00:01+08:00',{'exit_id':'x','lot_id':'a','quantity':100}))
    before = a.summary()
    with pytest.raises(ValueError):
        a.apply(fill('bad-fill','2026-05-19T10:00:02+08:00','sell','a',100,1000,exit_id='x'))
    assert a.summary() == before


@pytest.mark.parametrize('change',['price','capacity','intraday','bool_capacity'])
def test_suspension_cannot_mint_raw_prices_or_liquidity(change):
    a = IntegratedAccount(golden_scenarios()['suspension_exit']['config'])
    e = market('bad','2026-05-19T10:00:00+08:00',status='suspended')
    if change == 'price': e['value']['raw_price_fen'] = 1000
    elif change == 'capacity': e['value']['sell_capacity'] = 100
    elif change == 'intraday': e['value']['coverage'] = 'intraday'
    else: e['value']['buy_capacity'] = True
    with pytest.raises(ValueError):
        a.apply(e)


def test_pending_ex_cannot_be_bypassed_with_a_new_price():
    a,s = account_until(1)
    with pytest.raises(ValueError,match='due corporate accrual'):
        a.apply(s['events'][3])


def test_ex_day_halt_never_carries_pre_ex_price():
    a,_ = account_until(3)
    out = a.apply(market('ex-halt','2026-05-19T10:00:00+08:00',status='suspended'))
    assert out['marks'][0]['reason'] == 'pre_action_mark_invalidated'
    assert out['net_indicative_equity_fen'] is None


@pytest.mark.parametrize('change',['over_capacity','invented_price','expired_quote','no_exit','wrong_lot','bool_qty'])
def test_trade_constraints_and_failure_rollback(change):
    s = golden_scenarios()['suspension_exit']
    a = IntegratedAccount(s['config'])
    for e in s['events'][:4]: a.apply(e)
    e = deepcopy(s['events'][4])
    if change == 'over_capacity': e['value']['quantity'] = 100
    elif change == 'invented_price': e['value']['price_fen'] = 1000
    elif change == 'expired_quote': e['at'] = e['effective_at'] = '2026-05-22T10:02:00+08:00'
    elif change == 'no_exit': e['value']['exit_id'] = 'missing'
    elif change == 'wrong_lot': e['value']['lot_id'] = 'b'
    else: e['value']['quantity'] = True
    before = a.summary()
    with pytest.raises(ValueError): a.apply(e)
    assert a.summary() == before


def test_capacity_not_replenished_by_same_evidence_or_fill():
    s = golden_scenarios()['suspension_exit']
    a = IntegratedAccount(s['config'])
    for e in s['events'][:5]: a.apply(e)
    before = a.summary()
    assert a.apply(s['events'][4]) == before
    with pytest.raises(ValueError):
        a.apply({**s['events'][4],'event_id':'extra-same-capacity'})
    with pytest.raises(ValueError,match='replenish'):
        a.apply({**s['events'][3],'event_id':'same-evidence','at':'2026-05-22T10:00:02+08:00',
                 'effective_at':'2026-05-22T10:00:02+08:00'})
    assert a.summary() == before


def test_delivered_not_released_and_restricted_lot_cannot_sell():
    a,s = account_until(9)  # credited, not yet released
    aid = s['config']['actions'][0]['action_id']
    lid = 'entitlement-'+identity([aid,'a'])
    a.apply(market('delivery-day-raw','2026-05-21T11:00:00+08:00',price=700))
    a.apply(event('pending-exit','exit_intent','2026-05-21T11:00:01+08:00',{'exit_id':'u','lot_id':lid,'quantity':40}))
    before = a.summary()
    with pytest.raises(ValueError,match='not released'):
        a.apply(fill('unreleased','2026-05-21T11:00:02+08:00','sell',lid,40,700,exit_id='u'))
    assert a.summary() == before
    a.apply(s['events'][9]); a.apply(s['events'][10])
    restricted = 'entitlement-'+identity([aid,'b'])
    a.apply(event('restricted-exit','exit_intent','2026-05-22T11:00:01+08:00',{'exit_id':'r','lot_id':restricted,'quantity':80}))
    with pytest.raises(ValueError):
        a.apply(fill('restricted','2026-05-22T11:00:02+08:00','sell',restricted,40,750,exit_id='r'))


def test_new_buy_keeps_t_plus_boundary_and_does_not_enter_old_rights():
    a,_ = account_until(7)
    summary = a.summary()
    assert summary['lots']['new-after-record']['sellable_from'] == '2026-05-20'
    assert all(l['lot_id'] != 'new-after-record' for r in summary['rights'].values() for l in r['lots'])
    a.apply(event('exit-new','exit_intent','2026-05-19T10:00:04+08:00',{'exit_id':'new','lot_id':'new-after-record','quantity':100}))
    with pytest.raises(ValueError,match='not released or settled'):
        a.apply(fill('same-day-new','2026-05-19T10:00:05+08:00','sell','new-after-record',100,700,exit_id='new'))


def test_exit_intents_cannot_oversubscribe_or_fill_without_capacity():
    s = golden_scenarios()['suspension_exit']
    a = IntegratedAccount(s['config'])
    for e in s['events'][:2]: a.apply(e)
    with pytest.raises(ValueError,match='exceed'):
        a.apply(event('double-exit','exit_intent','2026-05-19T10:00:00+08:00',{'exit_id':'other','lot_id':'a','quantity':1}))


def test_parent_duplicate_conflict_clock_and_independent_replay():
    a,s = account_until(1)
    before = a.summary()
    assert a.apply(s['events'][0]) == before
    with pytest.raises(ValueError,match='idempotency'):
        a.apply({**s['events'][0],'evidence_ref':'changed'})
    with pytest.raises(ValueError,match='order'):
        a.apply({**s['events'][1],'at':'2026-05-18T16:00:00+08:00'})
    assert a.summary() == before
    assert replay(**s) == replay(**deepcopy(s))


def test_current_session_evidence_cannot_be_assumed_next_day():
    s = golden_scenarios()['suspension_exit']
    a = IntegratedAccount(s['config'])
    a.apply(s['events'][0])
    a.apply(event('next-day-intent','exit_intent','2026-05-20T09:30:00+08:00',{'exit_id':'later','lot_id':'a','quantity':100}))
    assert a.summary()['marks'][0]['reason'] == 'missing_current_session_evidence'
    assert a.summary()['net_indicative_equity_fen'] is None


def test_new_directory_bound_replay_and_tamper(tmp_path):
    folder = tmp_path/'run'
    result = run_scenarios(golden_scenarios(),folder)
    assert verify_scenarios(folder) == result
    with pytest.raises(FileExistsError):
        run_scenarios(golden_scenarios(),folder)
    (folder/'report.json').write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError):
        verify_scenarios(folder)


def test_delivery_preserves_marked_assets_not_double_credit():
    a,s = account_until(8)
    a.apply(market('pre-delivery-raw','2026-05-21T10:01:00+08:00',price=700))
    before = a.summary()
    assert before['share_receivable_quantity']['SZ.000001'] == 120
    after = a.apply(s['events'][8])
    assert before['net_indicative_equity_fen'] == after['net_indicative_equity_fen']
    assert after['account_reconciliation_residual_fen'] == 0


def test_tax_payment_changes_cash_and_liability_together_not_equity_twice():
    s = rights()
    a = IntegratedAccount(s['config'])
    for e in s['events'][:-1]: a.apply(e)
    before = a.summary()
    after = a.apply(s['events'][-1])
    assert after['cash_fen'] == before['cash_fen']-45
    assert after['known_tax_liability_fen'] == before['known_tax_liability_fen']-45
    assert after['fresh_equity_fen'] == before['fresh_equity_fen']
    assert after['available_cash_fen'] == before['available_cash_fen']


def test_zero_record_ownership_does_not_reward_later_buyer():
    s = rights()
    s['config'].update(lots=[],initial_marks={})
    a = IntegratedAccount(s['config'])
    a.apply(s['events'][0])
    assert not a.books and a.summary()['zero_rights_records']
    with pytest.raises(ValueError):
        a.apply(s['events'][3])  # ex-date acknowledgement still needed for mark epoch
    a.apply(s['events'][1]); a.apply(s['events'][3]); a.apply(s['events'][6])
    out = a.summary()
    assert out['holdings']['SZ.000001'] == 100 and out['cash_receivable_fen'] == 0
    assert not out['rights'] and not out['tax_unknown']
    assert out['share_receivable_quantity']['SZ.000001'] == 0
    with pytest.raises(ValueError):
        a.apply(s['events'][7])  # zero ownership cannot claim dividend receipt


@pytest.mark.parametrize('kind',['cash_dividend','capitalization'])
def test_parent_single_component_actions_do_not_create_other_component(kind):
    s = rights()
    terms = s['config']['actions'][0]
    terms['action_type'] = kind
    if kind == 'cash_dividend':
        terms.update(shares_per_share='0',share_delivery_date=None,share_listing_date=None)
    else:
        terms.update(gross_cny_per_share='0',cash_pay_date=None)
    a = IntegratedAccount(s['config'])
    a.apply(s['events'][0]); a.apply(s['events'][1])
    out = a.summary()
    if kind == 'cash_dividend':
        assert out['cash_receivable_fen'] == 750 and out['held_cost_fen'] == 340001
        assert out['share_receivable_cost_fen'] == 0
    else:
        assert out['cash_receivable_fen'] == 0 and out['share_receivable_cost_fen'] == 97143
        assert out['held_cost_fen'] == 242858


def test_odd_lot_and_calendar_tail_buy_constraints():
    s = golden_scenarios()['suspension_exit']
    s['config']['rules']['SZ.000001'].update(sell_lot=100,allow_odd_full_exit=False)
    a = IntegratedAccount(s['config'])
    for e in s['events'][:4]: a.apply(e)
    with pytest.raises(ValueError,match='odd-lot'):
        a.apply(s['events'][4])
    with pytest.raises(ValueError,match='calendar lacks'):
        a.apply(fill('tail-buy','2026-05-22T10:00:01+08:00','buy','tail',100,800))
