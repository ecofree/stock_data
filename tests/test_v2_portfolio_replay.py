from copy import deepcopy

import pytest

from trade_system.v2.portfolio_replay import POLICY,replay_variant
from trade_system.v2.portfolio_diagnostic import SCOPE


AT = '2026-09-10T10:00:00+00:00'
DAYS = ['2026-03-31','2026-04-01','2026-04-02','2026-04-03','2026-04-07']


def inputs():
    p = {'scope':SCOPE,'exposure_status':'previously_inspected_not_untouched',
        'fill_assumption':'unconstrained_next_open_and_due_close','calendar_assumption':'SSE_sessions_shared_not_exchange_certified',
        'missing_mark_policy':'stop_account','corporate_action_policy':'stop_on_held_factor_change',
        'initial_cash_fen':1000000,'top_k':1,'max_positions':2,'ticket_bps':5000,'buy_lot':100,'hold_sessions':2,
        't_plus_sessions':1,'fees':{'version':'fixture','commission_bps':'0','minimum_commission_fen':0,'transfer_bps':'0','sell_tax_bps':'0'}}
    pred = [[DAYS[0],'000001',2],[DAYS[0],'000002',1],[DAYS[2],'000001',1],[DAYS[2],'000002',2]]
    b = {'predictions':{'a':pred,'b':deepcopy(pred),'c':deepcopy(pred)},'calendar':DAYS,
        'identity_map':{'000001':'SZ.000001','000002':'SZ.000002'},'bars':[
            {'date':d,'instrument':code,'open_fen':1000,'close_fen':1100,'factor':'1'}
            for code,days in [('SZ.000001',DAYS[1:3]),('SZ.000002',DAYS[3:])] for d in days]}
    return b,p


def run(b=None,p=None,cases=None,prior=None):
    fb,fp = inputs()
    return replay_variant(b or fb,p or fp,POLICY,cases or [],prior or [],'a',repair_asof=AT)


def test_empty_account_full_horizon_two_selections():
    r = run()
    assert r['complete_requested_period'] and len(r['fills']) == 4
    assert r['initial_holdings'] == [] and len(r['daily']) == len(DAYS)
    assert r['final']['cash_fen'] == 1100000
    assert r['portfolio_return'] == '0.1'
    assert r['actual_operator_return'] is None and not r['execution_ready']
    assert r['final']['cash_reconciliation_residual_fen'] == 0


def test_missing_held_mark_blocks_later_entries_but_retains_full_horizon():
    b,p = inputs()
    b['bars'] = [x for x in b['bars'] if x['date'] != DAYS[2]]
    r = run(b,p)
    assert len(r['daily']) == len(DAYS) and not r['complete_requested_period']
    assert r['portfolio_return'] is None and r['final']['holdings']['SZ.000001'] == 500
    assert r['final']['held_cost_fen'] == 500000
    assert r['blocked_new_risk_since'] == DAYS[2]
    assert any(s['reason'] == 'account_risk_blocked' for s in r['skips'])


def test_known_later_close_exits_within_wait_horizon():
    b,p = inputs()
    b['bars'] = [x for x in b['bars'] if x['date'] != DAYS[2]]
    b['bars'].append({'date':DAYS[3],'instrument':'SZ.000001','open_fen':900,'close_fen':900,'factor':'1'})
    r = run(b,p)
    assert r['complete_requested_period'] and r['portfolio_return'] == '-0.05'
    assert len(r['fills']) == 2


def test_zero_rights_action_can_later_enter_at_ex_open():
    b,p = inputs()
    t = {'action_id':'zero','instrument':'SZ.000001','action_type':'cash_dividend',
        'record_date':DAYS[0],'ex_date':DAYS[1],'cash_pay_date':DAYS[1],
        'share_delivery_date':None,'share_listing_date':None,'gross_cny_per_share':'1','shares_per_share':'0',
        'available_at':AT,'evidence_refs':['fixture']}
    r = run(b,p,prior=[{'terms_schema_complete':True,'terms':t}])
    assert r['final']['zero_rights_records']['zero']['quantity'] == 0
    assert not r['final']['rights'] and r['complete_requested_period']


def test_record_close_positions_drive_cash_and_shares():
    b,p = inputs()
    c = {'case_id':'mixed','date':DAYS[2],'instrument':'SZ.000001','conflicting_fields':[],
        'native_cash_conflict_evidence_ids':[], 'nonconflicting_disclosure_values':{
        'record_date':DAYS[1],'ex_date':DAYS[2],'cash_pay_date':DAYS[2],'share_listing_date':DAYS[2],
        'gross_cny_per_share':'0.5','bonus_shares_per_share':'0','capitalization_shares_per_share':'0.4',
        'cash_payment_scope':'csdc_delegated_A_share_holders_not_self_distribution'}}
    b['bars'][1]['factor'] = '1.4'
    r = run(b,p,cases=[c])
    rights = r['final']['rights']['daily-case-mixed']
    assert rights['delivered_quantity'] == 200 and rights['gross_cash_fen'] == 25000
    assert rights['cash_received_fen'] == 20000
    assert r['complete_requested_period']
    assert r['final']['cost_reconciliation_residual_fen'] == 0


def test_missing_entry_does_not_create_holding_or_replacement():
    b,p = inputs()
    b['bars'] = [x for x in b['bars'] if x['date'] != DAYS[1]]
    r = run(b,p)
    assert len(r['fills']) == 2
    assert r['skips'][0]['reason'] == 'entry_observation_missing'


def test_factor_change_without_terms_retains_inventory():
    b,p = inputs()
    b['bars'][1]['factor'] = '2'
    r = run(b,p)
    assert not r['complete_requested_period']
    assert any(g['reason'] == 'unexplained_held_factor_change' for g in r['data_requests'])


def test_policy_and_variant_are_frozen():
    b,p = inputs()
    policy = deepcopy(POLICY)
    policy['exit_wait_sessions'] = 10
    with pytest.raises(ValueError,match='preregistered'):
        replay_variant(b,p,policy,[],[],'a',repair_asof=AT)
    with pytest.raises(ValueError,match='variant'):
        replay_variant(b,p,POLICY,[],[],'missing',repair_asof=AT)


def test_original_full_sample_preserved():
    b,p = inputs()
    before = deepcopy(b)
    run(b,p)
    assert b == before
