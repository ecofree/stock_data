from copy import deepcopy

import pytest

from trade_system.v2.domain import identity
from trade_system.v2.entitlements import EntitlementBook, replay, validate_terms
from trade_system.v2.entitlement_run import golden_scenario, run_scenario, verify_scenario


def test_hand_calculated_golden_controls():
    result = replay(**golden_scenario())
    ex, final = result['snapshots'][1], result['final']
    assert ex['cash_receivable_fen'] == 750 and ex['cash_received_fen'] == 0
    assert ex['share_receivable_quantity'] == 120 and ex['released_unrestricted_quantity'] == 0
    assert ex['tax_due_fen'] is None and ex['portfolio_return'] is None
    assert final['gross_cash_fen'] == 750 and final['net_cash_contribution_fen'] == 675
    assert final['cost_transfer_fen'] == 97143 and final['delivered_quantity'] == 120
    assert [l['parent_remaining_cost_fen'] for l in final['lots']] == [71429, 171429]
    assert final['released_unrestricted_quantity'] == 40  # restricted lot stays restricted
    assert final['settlement_complete'] and final['tax_due_fen'] == 0
    assert final['cash_residual_fen'] == final['cost_residual_fen'] == 0
    assert not any(final[k] for k in ('can_apply_to_account','can_resume_portfolio','execution_ready'))


def test_snapshot_is_frozen_and_not_payment_holdings():
    scenario = golden_scenario()
    book = EntitlementBook(scenario['config'])
    scenario['config']['snapshot']['lots'].clear()  # later parent disposal cannot delete rights
    assert book.apply(scenario['events'][0])['share_receivable_quantity'] == 120
    assert book.summary()['lots'][0]['acquired_at'] == '2026-04-01T10:00:00+08:00'


@pytest.mark.parametrize('field,value', [('scope','live'), ('policy','guessed')])
def test_real_account_and_unknown_policy_rejected(field, value):
    config = golden_scenario()['config']
    config[field] = value
    with pytest.raises(ValueError):
        EntitlementBook(config)


@pytest.mark.parametrize('field,value', [('record_date','2026-05-19'), ('ex_date','2026-05-17'),
    ('cash_pay_date',None), ('share_listing_date',None), ('share_delivery_date','2026-05-18'),
    ('gross_cny_per_share','NaN'), ('shares_per_share',True), ('action_type','split'), ('instrument','301232'),
    ('evidence_refs',[]), ('available_at','2026-05-18')])
def test_invalid_terms_fail_closed(field, value):
    terms = golden_scenario()['config']['terms']
    terms[field] = value
    with pytest.raises(ValueError):
        validate_terms(terms)


@pytest.mark.parametrize('change', ['early','duplicate','future','foreign','bool_qty','unknown_restriction'])
def test_record_snapshot_validation(change):
    config = golden_scenario()['config']
    snap = config['snapshot']
    if change == 'early':
        snap['asof'] = '2026-05-18T14:00:00+08:00'
    elif change == 'duplicate':
        snap['lots'][1]['lot_id'] = 'a'
    elif change == 'future':
        snap['lots'][0]['acquired_at'] = '2026-05-19T09:30:00+08:00'
    elif change == 'foreign':
        snap['lots'][0]['instrument'] = 'SH.600001'
    elif change == 'bool_qty':
        snap['lots'][0]['quantity'] = True
    else:
        snap['lots'][0]['restricted'] = None
    with pytest.raises(ValueError):
        EntitlementBook(config)


def test_fractional_registry_allocation_stops_atomically():
    scenario = golden_scenario()
    scenario['config']['snapshot']['lots'][1]['quantity'] = 201
    book = EntitlementBook(scenario['config'])
    before = book.summary()
    with pytest.raises(ValueError, match='fractional'):
        book.apply(scenario['events'][0])
    assert book.summary() == before and not book.seen


def test_account_cash_rounding_once_not_per_lot():
    scenario = golden_scenario()
    scenario['config']['terms']['shares_per_share'] = '0'
    scenario['config']['terms']['action_type'] = 'cash_dividend'
    scenario['config']['terms']['share_delivery_date'] = None
    scenario['config']['terms']['share_listing_date'] = None
    for lot in scenario['config']['snapshot']['lots']:
        lot['quantity'] = 1
    book = EntitlementBook(scenario['config'])
    result = book.apply(scenario['events'][0])
    assert result['gross_cash_fen'] == 5
    assert [r['gross_cash_fen'] for r in result['lots']] == [3,2]


def test_duplicate_replay_conflict_and_duplicate_business_effect():
    s = golden_scenario()
    book = EntitlementBook(s['config'])
    first = book.apply(s['events'][0])
    assert book.apply(s['events'][0]) == first
    with pytest.raises(ValueError, match='idempotency'):
        book.apply({**s['events'][0], 'evidence_ref': 'different'})
    with pytest.raises(ValueError):
        book.apply({**s['events'][0], 'event_id': 'duplicate-business'})
    assert book.summary() == first


@pytest.mark.parametrize('index,change', [(1,'amount'),(1,'early'),(2,'quantity'),(2,'bool'),(2,'early'),
                                        (3,'early'),(4,'tax'),(5,'tax')])
def test_bad_settlement_is_atomic(index, change):
    s = golden_scenario()
    book = EntitlementBook(s['config'])
    for event in s['events'][:index]:
        book.apply(event)
    event = deepcopy(s['events'][index])
    if change == 'early':
        event['effective_at'] = '2026-05-19T11:00:00+08:00'
    elif change == 'amount':
        event['value']['net_fen'] += 1
    elif change in ('quantity','bool'):
        event['value']['by_lot']['a'] = 41 if change == 'quantity' else True
    elif index == 4:
        event['value']['total_fen'] = 29
    else:
        event['value']['amount_fen'] = 46
    before = book.summary()
    with pytest.raises(ValueError):
        book.apply(event)
    assert book.summary() == before


def test_announcement_and_listing_date_are_not_receipt_or_release():
    s = golden_scenario()
    book = EntitlementBook(s['config'])
    book.apply(s['events'][0])
    with pytest.raises(ValueError):
        book.apply(s['events'][3])
    assert book.summary()['delivered_quantity'] == 0
    book.apply(s['events'][2])
    assert book.summary()['released_unrestricted_quantity'] == 0
    assert book.summary()['tax_status'] == 'unknown_not_zero'


def test_capitalization_separate_from_bonus_without_tax_inference():
    s = golden_scenario()
    terms = s['config']['terms']
    terms.update(action_type='capitalization', gross_cny_per_share='0', cash_pay_date=None)
    book = EntitlementBook(s['config'])
    book.apply(s['events'][0])
    assert book.summary()['cash_receivable_fen'] == 0
    assert book.summary()['tax_due_fen'] is None
    book.apply(s['events'][2])
    assert not book.summary()['settlement_complete']


def test_unknown_tax_partial_payment_and_event_clock():
    s = golden_scenario()
    book = EntitlementBook(s['config'])
    for e in s['events'][:5]:
        book.apply(e)
    payment = s['events'][5]
    payment['value']['amount_fen'] = 20
    assert book.apply(payment)['tax_due_fen'] == 25
    assert not book.summary()['settlement_complete']
    before = book.summary()
    with pytest.raises(ValueError):
        book.apply({**payment, 'event_id': 'backdate', 'at': '2026-05-21T10:00:00+08:00'})
    with pytest.raises(ValueError):
        book.apply({**payment, 'event_id': 'future', 'effective_at': '2026-05-23T10:00:00+08:00'})
    assert book.summary() == before


def test_source_bound_package_replay_and_tamper(tmp_path):
    result = run_scenario(golden_scenario(), tmp_path/'run')
    assert verify_scenario(tmp_path/'run') == result
    with pytest.raises(FileExistsError):
        run_scenario(golden_scenario(), tmp_path/'run')
    path = tmp_path/'run'/'report.json'
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError):
        verify_scenario(tmp_path/'run')


def test_stream_identity_and_restart_are_stable():
    s = golden_scenario()
    first = replay(**s)
    assert first == replay(**deepcopy(s)) and first['events_id'] == identity(s['events'])
