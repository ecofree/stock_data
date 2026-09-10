from copy import deepcopy
import json

import pytest

from trade_system.v2 import daily_account as daily
from trade_system.v2 import daily_case_run as runner
from trade_system.v2.domain import canonical, identity
from trade_system.v2.integrated_account import IntegratedAccount
from trade_system.v2.integrated_run import golden_scenarios,replay
from trade_system.v2.gap_evidence import read_json
from trade_system.v2.rolling_research import file_hash


AT = '2026-09-10T09:00:00+00:00'
CODE = 'SZ.000001'
CID = 'a'*64
DAYS = ['2026-05-18','2026-05-19','2026-05-20','2026-05-21']


def fixture(kind='mixed', resume_price=False):
    bars = [{'date':DAYS[0],'instrument':CODE,'open_fen':1000,'close_fen':1000,'factor':'1'}]
    values = {'halt_start_date':DAYS[1]}
    if kind == 'mixed':
        values = {'record_date':DAYS[0],'ex_date':DAYS[1],'cash_pay_date':DAYS[1],
            'gross_cny_per_share':'0.5','bonus_shares_per_share':'0','capitalization_shares_per_share':'0.3',
            'share_listing_date':DAYS[1],
            'cash_payment_scope':'csdc_delegated_A_share_holders_not_self_distribution'}
        bars.append({'date':DAYS[1],'instrument':CODE,'open_fen':800,'close_fen':800,'factor':'1.3'})
    elif kind == 'closed':
        values.update(halt_end_date_inclusive=DAYS[1],resumption_date=DAYS[2])
        if resume_price:
            bars.append({'date':DAYS[2],'instrument':CODE,'open_fen':800,'close_fen':800,'factor':'1'})
    bundle = {'calendar':DAYS,'bars':bars}
    case = {'case_id':CID,'instrument':CODE,'date':DAYS[1],
        'nonconflicting_disclosure_values':values,'conflicting_fields':[],'native_cash_conflict_evidence_ids':[]}
    return bundle,case


def run(kind='mixed',resume_price=False,**kwargs):
    b,c = fixture(kind,resume_price)
    return daily.diagnose_case(kwargs.get('bundle',b),kwargs.get('case',c),repair_asof=AT,
                               assumptions=kwargs.get('assumptions',daily.ASSUMPTIONS))


def test_cash_capitalization_uses_same_ledger_without_synthetic_relabel():
    b,c = fixture()
    before = deepcopy([b,c])
    r = run(bundle=b,case=c)
    assert [b,c] == before
    assert r['case_hypothesis_complete']
    assert r['config']['scope'] == daily.SCOPE
    assert r['config']['actions'][0]['action_type'] == 'cash_and_capitalization'
    assert all(e['at'] == AT and e['evidence_ref'].startswith('assumption:') for e in r['events'])
    assert not any(e['kind'] == 'synthetic_fill' for e in r['events'])
    f = r['final']
    assert f['holdings'][CODE] == 0 and sum(x['quantity'] for x in f['fills']) == 130
    assert f['cash_fen'] == 10106947
    assert f['cash_reconciliation_residual_fen'] == f['cost_reconciliation_residual_fen'] == 0
    assert f['share_reconciliation_residual'][CODE] == 0
    rights = next(iter(f['rights'].values()))
    assert rights['scope'] == daily.HistoricalRights.scope
    assert rights['tax_assessed_fen'] == 1000 and rights['cash_received_fen'] == 4000
    assert r['portfolio_return'] is None and r['selection_advantage'] is None
    assert not r['knowledge_replay_validated'] and not r['execution_ready']
    assert r['assumed_source_fields'][0]['source_value'] is None
    assert 'share_delivery_date' not in c['nonconflicting_disclosure_values']


def test_closed_halt_with_missing_resume_price_keeps_inventory_and_null_return():
    r = run('closed')
    assert [d['status_used'] for d in r['daily']] == ['suspended','unknown','unknown']
    assert r['final']['holdings'][CODE] == 100
    assert r['final']['fresh_equity_fen'] is None and r['hypothetical_case_return'] is None
    assert not r['case_hypothesis_complete'] and not r['final']['fills']


def test_closed_halt_known_raw_resume_can_hypothetically_exit():
    r = run('closed',True)
    assert r['case_hypothesis_complete']
    assert len(r['final']['fills']) == 1
    assert r['final']['fills'][0]['effective_at'].startswith(DAYS[2])
    assert r['final']['fills'][0]['actual_fill'] is False


def test_unknown_halt_end_is_not_infinite_suspension_or_delisting():
    r = run('open')
    assert [d['status_used'] for d in r['daily']] == ['suspended','unknown','unknown']
    assert r['final']['holdings'][CODE] == 100 and r['final']['held_cost_fen'] == 100000
    assert r['final']['cash_fen'] == daily.ASSUMPTIONS['initial_cash_fen']
    assert r['hypothetical_case_return'] is None
    assert r['daily'][0]['summary']['marks'][0]['basis'] == 'indicative_suspended_carry_not_tradable'
    assert r['daily'][1]['summary']['marks'][0]['price_fen'] is None


def test_delayed_cash_does_not_become_cash_on_ex_date():
    b,c = fixture()
    c['nonconflicting_disclosure_values']['cash_pay_date'] = DAYS[2]
    r = run(bundle=b,case=c)
    assert r['daily'][0]['summary']['cash_receivable_fen'] == 5000
    assert r['daily'][1]['summary']['cash_receivable_fen'] == 0
    assert r['case_hypothesis_complete']


def test_share_delivery_is_not_assumed_before_declared_listing():
    b,c = fixture()
    c['nonconflicting_disclosure_values']['share_listing_date'] = DAYS[2]
    r = run(bundle=b,case=c)
    assert r['daily'][0]['summary']['share_receivable_quantity'][CODE] == 30
    assert r['daily'][1]['summary']['holdings'][CODE] == 30
    assert not r['case_hypothesis_complete'] and r['hypothetical_case_return'] is None


def test_unexplained_later_factor_change_blocks_fill_and_mark():
    b,c = fixture('closed',True)
    b['bars'][-1]['factor'] = '2'
    r = run(bundle=b,case=c)
    assert r['daily'][1]['unexplained_factor_change']
    assert not r['final']['fills']


def test_conflicting_halt_and_raw_trading_does_not_fill():
    b,c = fixture('open')
    b['bars'].append({'date':DAYS[1],'instrument':CODE,'open_fen':800,'close_fen':800,'factor':'1'})
    r = run(bundle=b,case=c)
    assert r['daily'][0]['status_used'] == 'unknown' and not r['final']['fills']


@pytest.mark.parametrize('change', ['tax','exit_wait_sessions','capacity'])
def test_policy_change_requires_new_version(change):
    policy = deepcopy(daily.ASSUMPTIONS)
    policy[change] = 'changed'
    with pytest.raises(ValueError,match='registered'):
        run(assumptions=policy)


@pytest.mark.parametrize('field', ['record_date','bonus_shares_per_share','cash_payment_scope','share_listing_date'])
def test_missing_required_source_terms_not_inferred(field):
    b,c = fixture()
    del c['nonconflicting_disclosure_values'][field]
    with pytest.raises(ValueError):
        run(bundle=b,case=c)


def test_source_conflict_stops_before_account_mutation():
    b,c = fixture()
    c['native_cash_conflict_evidence_ids'] = ['conflict']
    with pytest.raises(ValueError,match='conflict'):
        run(bundle=b,case=c)


def test_fractional_shares_not_rounded_locally():
    b,c = fixture()
    c['nonconflicting_disclosure_values']['capitalization_shares_per_share'] = '0.333'
    with pytest.raises(ValueError,match='fractional'):
        run(bundle=b,case=c)


def test_historical_account_rejects_synthetic_scope_and_event_backdating():
    r = run()
    with pytest.raises(ValueError):
        IntegratedAccount(r['config'])
    account = daily.HistoricalAccount(r['config'],repair_asof=AT,assumptions=daily.ASSUMPTIONS)
    event = deepcopy(r['events'][0])
    event['at'] = event['effective_at']
    with pytest.raises(ValueError,match='repair-time'):
        account.apply(event)
    event = deepcopy(r['events'][-1])
    event['kind'] = 'synthetic_fill'
    with pytest.raises(ValueError,match='masquerade'):
        account.apply(event)


def test_existing_golden_account_numbers_unchanged():
    results = {k:replay(**v)['final'] for k,v in golden_scenarios().items()}
    assert results['entitlement_parent']['fresh_equity_fen'] == 1315650
    assert results['suspension_exit']['cash_fen'] == 179980
    assert results['delisted_retention']['holdings']['SZ.000001'] == 100


def fake_parent(monkeypatch,tmp_path):
    b,c = fixture()
    payload = {'fields':{'cases':[c],'repair_asof':AT,'variant_counts':{'a':1,'b':1,'c':1},'potential_windows':3},
               'bundle':b,'parent_sha256':'source','adapter_sha256':'adapter'}
    monkeypatch.setattr(runner,'legacy_snapshot',lambda _:deepcopy(payload))
    plan = {'scope':daily.SCOPE,'parent':str(tmp_path/'old'),'legacy_wheel':'old.whl','legacy_wheel_sha256':'b'*64,
            'case_ids':[CID],'assumptions':deepcopy(daily.ASSUMPTIONS),'exposure_status':'previously_inspected_not_untouched'}
    return plan,payload


def reseal(folder):
    path = folder/'run/completed.json'
    m = read_json(path)[0]
    m.pop('manifest_id')
    m['artifact_hashes'] = {p.name:file_hash(p) for p in path.parent.iterdir() if p != path}
    path.write_text(canonical({**m,'manifest_id':identity(m)}),encoding='utf-8')


def test_registration_run_verify_and_no_repeat(monkeypatch,tmp_path):
    plan,_ = fake_parent(monkeypatch,tmp_path)
    out = tmp_path/'new'
    runner.register_cases(plan,out,clock=lambda:AT)
    assert not (out/'run').exists()
    result = runner.run_cases(out)
    assert runner.verify_cases(out) == result
    assert result['evaluated_strategy_variants'] == 0
    assert len(result['parent_variants_preserved']) == 3
    with pytest.raises(FileExistsError):
        runner.run_cases(out)
    with pytest.raises(FileExistsError):
        runner.register_cases(plan,out)


@pytest.mark.parametrize('target', ['summary.json',CID+'.json','review.md','started.json'])
def test_resealed_output_tampering_rejected(monkeypatch,tmp_path,target):
    plan,_ = fake_parent(monkeypatch,tmp_path)
    out = tmp_path/'new'
    runner.register_cases(plan,out,clock=lambda:AT)
    runner.run_cases(out)
    path = out/'run'/target
    if target.endswith('.md'):
        path.write_text('approved',encoding='utf-8')
    else:
        value = read_json(path)[0]
        value['execution_ready'] = True
        path.write_text(json.dumps(value),encoding='utf-8')
    reseal(out)
    with pytest.raises(ValueError):
        runner.verify_cases(out)


def test_parent_change_prevents_run(monkeypatch,tmp_path):
    plan,payload = fake_parent(monkeypatch,tmp_path)
    out = tmp_path/'new'
    runner.register_cases(plan,out,clock=lambda:AT)
    payload['parent_sha256'] = 'changed'
    with pytest.raises(ValueError,match='source binding'):
        runner.run_cases(out)
    assert not (out/'run').exists()


def test_unknown_case_not_registered(monkeypatch,tmp_path):
    plan,_ = fake_parent(monkeypatch,tmp_path)
    plan['case_ids'] = ['c'*64]
    with pytest.raises(ValueError,match='scope'):
        runner.register_cases(plan,tmp_path/'new',clock=lambda:AT)


def test_old_wheel_digest_required_before_extraction(tmp_path):
    wheel = tmp_path/'old.whl'
    wheel.write_bytes(b'not a wheel')
    with pytest.raises(ValueError,match='wheel changed'):
        runner.legacy_snapshot({'legacy_wheel':str(wheel),'legacy_wheel_sha256':'0'*64})


def test_malformed_duplicate_case_ids_rejected(monkeypatch,tmp_path):
    plan,_ = fake_parent(monkeypatch,tmp_path)
    plan['case_ids'] = [CID,CID]
    with pytest.raises(ValueError,match='unique'):
        runner.validate_plan(plan)


def test_case_horizon_must_be_available():
    b,c = fixture()
    b['calendar'] = b['calendar'][:-1]
    with pytest.raises(ValueError,match='horizon'):
        run(bundle=b,case=c)
