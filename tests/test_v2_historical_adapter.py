from copy import deepcopy
from pathlib import Path

import pytest

from trade_system.v2 import historical_adapter as adapter
from trade_system.v2.domain import canonical,identity
from trade_system.v2.gap_evidence import ingest_response,write_json,sha
from trade_system.v2.portfolio_diagnostic import SCOPE


KNOWN = '2026-09-10T08:00:00+00:00'
REPAIR = '2026-09-10T09:00:00+00:00'
PAST = '2026-04-23T18:00:00+08:00'


def fixture():
    policy = {'scope':SCOPE,'exposure_status':'previously_inspected_not_untouched',
        'fill_assumption':'unconstrained_next_open_and_due_close',
        'calendar_assumption':'SSE_sessions_shared_not_exchange_certified',
        'missing_mark_policy':'stop_account','corporate_action_policy':'stop_on_held_factor_change',
        'initial_cash_fen':1000000,'top_k':1,'max_positions':2,'ticket_bps':5000,
        'buy_lot':100,'hold_sessions':2,'t_plus_sessions':1,
        'fees':{'version':'fixture','commission_bps':'0','minimum_commission_fen':0,'transfer_bps':'0','sell_tax_bps':'0'}}
    bundle = {'predictions':{'left':[['2026-04-21','000001',2]],'right':[['2026-04-21','000001',1]]},
        'calendar':['2026-04-21','2026-04-22','2026-04-23'],'identity_map':{'000001':'SZ.000001'},
        'bars':[{'date':'2026-04-23','instrument':'SZ.000001','open_fen':1000,'close_fen':1100,'factor':'1'}]}
    case = {'case_id':'missing','date':'2026-04-22','instrument':'SZ.000001'}
    return bundle,policy,{'cases':[case]}


def stage(bundle=None,policy=None,audit=None,evidence=None,documents=None,**kwargs):
    b,p,a = fixture()
    return adapter.stage_inputs(bundle if bundle is not None else b,policy if policy is not None else p,
        audit if audit is not None else a,evidence or [],documents or [],
        artifact_available_at=KNOWN,repair_asof=REPAIR,replay_asof=kwargs.get('replay_asof',PAST))


def evidence(tmp_path,claim):
    raw = canonical({'schema_version':1,'rows':[claim]}).encode()
    (tmp_path/'raw.json').write_bytes(raw)
    receipt = {'schema_version':1,'source_family':'other','source_api':'fixture','origin_family':'fixture',
        'reference':'synthetic','source_revision':'fixture','source_event_at':KNOWN,'provider_received_at':KNOWN,
        'license_status':'unverified','row_index':0,'raw_sha256':sha(raw)}
    write_json(tmp_path/'receipt.json',receipt)
    return ingest_response(tmp_path/'receipt.json',tmp_path/'raw.json',tmp_path/'evidence',clock=lambda:KNOWN)


def test_full_variants_windows_and_observations_preserved_without_events():
    b,p,a = fixture()
    original = deepcopy(b)
    out = stage(b,p,a)
    r = out['report']
    assert b == original
    assert r['variant_counts'] == {'left':1,'right':1}
    assert r['potential_windows'] == 2 and r['requested_raw_identities'] == 2
    assert r['retained_raw_bars'] == 1 and r['missing_raw_identities'] == 1
    assert len(r['cases'][0]['window_ids']) == 2
    assert r['prediction_value_sha256'] == identity(b['predictions'])
    assert r['full_oos_identity_sha256'] == identity([['2026-04-21','000001']])
    assert r['emitted_parent_events'] == 0 and not r['new_experiment_registered']
    assert not r['execution_ready'] and not r['historical_diagnostic_ready']
    assert r['portfolio_return'] is None and not r['frozen_artifact_available_by_replay_cutoff']
    assert out['daily_observations']['rows'][0]['historical_row_available_at'] is None
    assert not out['daily_observations']['rows'][0]['parent_market_event_ready']


def test_late_suspension_is_repair_evidence_not_historical_knowledge(tmp_path):
    row = evidence(tmp_path,{'date':'2026-04-22','instrument':'SZ.000001','kind':'session_status',
                            'value':{'status':'suspended','coverage':'full_session'}})
    r = stage(evidence=[row])['report']
    assert r['repair_classification_counts'] == {'suspension_supported_not_authenticated':1}
    assert r['replay_classification_counts'] == {'unknown':1}
    assert 'case_day_session_explanation' not in r['cases'][0]['missing_source_fields']
    assert 'structured_exact_suspension_span' in r['cases'][0]['missing_source_fields']
    assert not r['cases'][0]['historical_repair_decision']['can_forward_fill_mark']


@pytest.mark.parametrize('change',['mismatched_variant','duplicate_prediction','nan_score','bool_score','wrong_exchange',
    'unmapped_code','extra_mapping','missing_calendar','duplicate_bar','adjusted_field','negative_price','invalid_factor',
    'label_column','unrequested_bar','incomplete_horizon','new_gap','duplicate_case'])
def test_frozen_input_invariants_fail_closed(change):
    b,p,a = fixture()
    if change == 'mismatched_variant': b['predictions']['right'] = [['2026-04-22','000001',1]]
    elif change == 'duplicate_prediction': b['predictions']['left'] *= 2
    elif change == 'nan_score': b['predictions']['left'][0][2] = float('nan')
    elif change == 'bool_score': b['predictions']['left'][0][2] = True
    elif change == 'wrong_exchange': b['identity_map']['000001'] = 'SZ.000002'
    elif change == 'unmapped_code': b['predictions']['left'][0][1] = '000002'
    elif change == 'extra_mapping': b['identity_map']['000002'] = 'SZ.000002'
    elif change == 'missing_calendar': b['calendar'] = ['2026-04-23','2026-04-21']
    elif change == 'duplicate_bar': b['bars'] *= 2
    elif change == 'adjusted_field': b['bars'][0]['adjustment'] = 'forward'
    elif change == 'negative_price': b['bars'][0]['open_fen'] = -1
    elif change == 'invalid_factor': b['bars'][0]['factor'] = 'nan'
    elif change == 'label_column': b['predictions']['left'][0].append(0.2)
    elif change == 'unrequested_bar': b['bars'].append({**b['bars'][0],'date':'2026-04-21'})
    elif change == 'incomplete_horizon': b['calendar'].pop()
    elif change == 'new_gap': b['bars'].clear()
    else: a['cases'] *= 2
    with pytest.raises(ValueError): stage(b,p,a)


def factor_fixture():
    b,p,a = fixture()
    b['bars'].insert(0,{**b['bars'][0],'date':'2026-04-22','factor':'0.7'})
    a['cases'] = [{'case_id':'factor','date':'2026-04-23','instrument':'SZ.000001'}]
    return b,p,a


def test_cash_component_never_fills_unknown_share_or_registration_fields(tmp_path):
    b,p,a = factor_fixture()
    row = evidence(tmp_path,{'date':'2026-04-23','instrument':'SZ.000001','kind':'corporate_action',
        'value':{'action_id':'cash','action_type':'cash_dividend','ex_date':'2026-04-23','gross_cny_per_share':'0.5'}})
    out = stage(b,p,a,evidence=[row])
    assert out['report']['action_candidates'] == 1
    assert out['report']['complete_action_term_candidates'] == 0
    assert {'record_date','cash_pay_date','complete_share_component_declaration'} <= set(out['report']['cases'][0]['missing_source_fields'])
    candidate = out['action_candidates']['rows'][0]
    assert 'shares_per_share' not in candidate['terms'] and 'record_date' not in candidate['terms']
    assert not candidate['system_replay_eligible'] and not candidate['can_apply_to_account']
    assert 'record_close_lots_from_new_parent_replay' in out['report']['cases'][0]['derive_only_after_new_replay']


def terms_document():
    return {'available_at':KNOWN,'document':{'kind':'entitlement_terms','date':'2026-04-23','instrument':'SZ.000001'},
        'terms':{'action_id':'cap','instrument':'SZ.000001','action_type':'capitalization','record_date':'2026-04-22',
            'ex_date':'2026-04-23','cash_pay_date':None,'share_delivery_date':'2026-04-23',
            'share_listing_date':'2026-04-23','gross_cny_per_share':'0','shares_per_share':'0.4',
            'available_at':KNOWN,'evidence_refs':['fixture-document']}}


def test_complete_terms_are_not_actual_account_receipts():
    b,p,a = factor_fixture()
    out = stage(b,p,a,documents=[terms_document()])
    assert out['report']['complete_action_term_candidates'] == 1
    candidate = out['action_candidates']['rows'][0]
    assert not candidate['account_receipt_proven'] and not candidate['system_replay_eligible']
    assert out['report']['repair_classification_counts'] == {'unknown':1}
    assert candidate['terms']['shares_per_share'] == '0.4'  # never infer from 0.7 -> 1 factor ratio


def test_conflicting_disclosure_revisions_are_not_silently_preferred():
    b,p,a = factor_fixture()
    d = terms_document()
    other = deepcopy(d)
    other['terms']['shares_per_share'] = '0.5'
    out = stage(b,p,a,documents=[d,other])
    assert len(out['action_candidates']['rows']) == 2
    assert 'resolve_corporate_action_component_or_revision_conflict' in out['report']['cases'][0]['missing_source_fields']


def test_future_terms_excluded_and_zero_prices_remain_unknown():
    b,p,a = factor_fixture()
    d = terms_document()
    d['available_at'] = '2026-09-11T08:00:00+00:00'
    assert stage(b,p,a,documents=[d])['report']['action_candidates'] == 0
    b,p,a = fixture()
    b['bars'][0]['open_fen'] = 0
    a['cases'].append({'case_id':'zero','date':'2026-04-23','instrument':'SZ.000001'})
    out = stage(b,p,a)
    assert 'positive_unadjusted_open_and_close' in out['report']['cases'][1]['missing_source_fields']
    assert out['daily_observations']['rows'][0]['open_fen'] == 0


def test_recent_replay_cutoff_does_not_certify_per_row_knowledge():
    out = stage(replay_asof=REPAIR)
    assert out['report']['frozen_artifact_available_by_replay_cutoff']
    assert out['report']['daily_rows_with_proven_historical_availability'] == 0


def test_package_full_bytes_replay_and_resealed_semantic_tamper(tmp_path,monkeypatch):
    b,p,a = fixture()
    raw = canonical(b).encode()
    binding = {'input_sha256':sha(raw)}
    payload = ({'policy':p,'registered_at':KNOWN},a,{'documents':[]},[],b,raw,binding)
    monkeypatch.setattr(adapter,'_load_parents',lambda plan:deepcopy(payload))
    plan = {'scope':adapter.SCOPE,'mode':adapter.MODE,'portfolio':'fixture','audit':'fixture','supplement':'fixture','replay_asof':PAST}
    folder = tmp_path/'run'
    result = adapter.build_adapter(plan,folder,clock=lambda:REPAIR)
    assert (folder/'frozen_inputs.json').read_bytes() == raw
    assert adapter.verify_adapter(folder) == result
    with pytest.raises(FileExistsError): adapter.build_adapter(plan,folder,clock=lambda:REPAIR)
    report,_ = adapter.read_json(folder/'report.json')
    report['emitted_parent_events'] = 10
    (folder/'report.json').write_text(canonical(report),encoding='utf-8')
    manifest,_ = adapter.read_json(folder/'completed.json')
    manifest.pop('manifest_id')
    manifest['artifact_hashes']['report.json'] = adapter.file_hash(folder/'report.json')
    (folder/'completed.json').write_text(canonical({**manifest,'manifest_id':identity(manifest)}),encoding='utf-8')
    with pytest.raises(ValueError,match='does not reproduce'): adapter.verify_adapter(folder)


def test_source_changes_during_build_never_seal_success(tmp_path,monkeypatch):
    b,p,a = fixture()
    raw = canonical(b).encode()
    calls = []
    def load(plan):
        calls.append(1)
        return {'policy':p,'registered_at':KNOWN},a,{'documents':[]},[],b,raw,{'input_sha256':str(len(calls))}
    monkeypatch.setattr(adapter,'_load_parents',load)
    plan = {'scope':adapter.SCOPE,'mode':adapter.MODE,'portfolio':'fixture','audit':'fixture','supplement':'fixture','replay_asof':PAST}
    with pytest.raises(ValueError,match='changed during'):
        adapter.build_adapter(plan,tmp_path/'run',clock=lambda:REPAIR)
    assert not (tmp_path/'run/completed.json').exists()


def test_real_plan_targets_existing_frozen_packages():
    root = Path(__file__).resolve().parents[1]
    plan,_ = adapter.read_json(root/'docs/v2/historical_adapter_20260910.json')
    assert plan['scope'] == adapter.SCOPE and plan['mode'] == adapter.MODE
    assert plan['replay_asof'] == '2026-07-29T18:00:00+08:00'
