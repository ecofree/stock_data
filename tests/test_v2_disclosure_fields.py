from copy import deepcopy

import pytest

from trade_system.v2 import disclosure_fields as fields
from trade_system.v2.domain import canonical, identity
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.rolling_research import file_hash


CREATED = '2026-09-10T08:00:00+00:00'
PAST = '2026-07-29T10:00:00+00:00'
PDF = b'%PDF-1.4\nsynthetic fixture, not a real announcement'


def document(kind='distribution'):
    values = ({'record_date':'2026-05-14','ex_date':'2026-05-15','cash_pay_date':'2026-05-15',
        'gross_cny_per_share':'0.57','bonus_shares_per_share':'0','capitalization_shares_per_share':'0',
        'cash_payment_scope':'csdc_delegated_A_share_holders_not_self_distribution',
        'share_delivery_date':None,'share_listing_date':None} if kind == 'distribution' else
        {'halt_start_date':'2026-05-15','halt_end_date_inclusive':None,'resumption_date':None,'terminal_date':None})
    return {'id':'fixture','url':'https://static.cninfo.com.cn/fixture.PDF','title':'Synthetic document',
        'published_date':'2026-05-13','instrument':'SZ.000001','date':'2026-05-15','kind':kind,
        'archive_id':None,'method':fields.METHOD,'notes':'Not real evidence.',
        'fields':{k:{'value':v,'pages':[1] if v is not None else [],'basis':'Explicit fixture or unknown'} for k,v in values.items()}}


def parent(kind='distribution'):
    missing = (['record_date','cash_pay_date','complete_share_component_declaration'] if kind == 'distribution' else
               ['structured_exact_suspension_span','structured_resumption_or_terminal_lifecycle','post_resumption_raw_prices'])
    return {'repair_asof':CREATED,'replay_asof':PAST,'variant_counts':{'a':20,'b':20,'c':20},'potential_windows':30,
        'replay_classification_counts':{'unknown':1},'cases':[{'case_id':'case','instrument':'SZ.000001','date':'2026-05-15',
        'window_ids':['a','b','c'],'missing_source_fields':missing+['independent_source_and_use_rights_review'],
        'missing_declared_policies':['tax_policy'],'derive_only_after_new_replay':['record_lots'],
        'problems':['missing_raw_bar'] if kind == 'halt' else ['factor_change_needs_entitlement_not_ratio_inference']}]}


def record(doc=None, at=CREATED):
    return fields._record(doc or document(), 'digest',at,CREATED)


def stage(docs=None, kind='distribution', actions=None, **kwargs):
    return fields.stage_fields(parent(kind),actions or [],docs if docs is not None else [record(document(kind))],
        repair_asof=kwargs.get('repair_asof',CREATED),replay_asof=kwargs.get('replay_asof',PAST))


def test_cash_dates_and_explicit_zero_components_map_without_account_authority():
    r = stage()
    c = r['cases'][0]
    assert r['mapped_fields_pending_review'] == 3
    assert c['total_new_shares_per_share'] == '0'
    assert c['missing_declared_policies'] == ['tax_policy']
    assert c['derive_only_after_new_replay'] == ['record_lots']
    assert not c['can_apply_to_account'] and not r['execution_ready']
    assert r['portfolio_return'] is None and r['emitted_parent_events'] == 0
    assert r['potential_windows'] == 30 and len(r['variant_counts']) == 3
    assert r['historically_available_new_documents'] == 0
    assert r['parent_replay_classification_counts'] == {'unknown':1}


def test_unknown_share_component_never_becomes_zero():
    d = document()
    d['fields']['capitalization_shares_per_share']['value'] = None
    c = stage([record(d)])['cases'][0]
    assert c['total_new_shares_per_share'] is None
    assert 'complete_share_component_declaration' in c['remaining_source_fields']


def native(cash='0.57'):
    return {'case_id':'case','source':'native_cash_component','available_at':CREATED,
            'evidence_id':'native','terms':{'gross_cny_per_share':cash}}


def test_cash_plus_capitalization_is_not_cash_only_or_assumed_delivery():
    d = document()
    d['fields']['capitalization_shares_per_share']['value'] = '0.3'
    c = stage([record(d)],actions=[native()])['cases'][0]
    assert c['total_new_shares_per_share'] == '0.3'
    assert c['native_cash_component_was_not_complete_action']
    assert 'explicit_share_delivery_date' in c['remaining_source_fields']
    assert 'explicit_share_listing_date' in c['remaining_source_fields']
    assert not c['native_cash_conflict_evidence_ids']


def test_open_halt_does_not_infer_future_suspension_or_terminal_state():
    c = stage(kind='halt')['cases'][0]
    assert not c['explicit_closed_halt_candidate']
    assert c['nonconflicting_disclosure_values'] == {'halt_start_date':'2026-05-15'}
    assert 'structured_exact_suspension_span' in c['remaining_source_fields']


def test_closed_halt_maps_span_but_retains_missing_post_resume_prices():
    d = document('halt')
    for key,value in [('halt_end_date_inclusive','2026-05-15'),('resumption_date','2026-05-18')]:
        d['fields'][key] = {'value':value,'pages':[1],'basis':'Explicit fixture'}
    c = stage([record(d)],kind='halt')['cases'][0]
    assert c['explicit_closed_halt_candidate']
    assert 'structured_exact_suspension_span' not in c['remaining_source_fields']
    assert 'post_resumption_raw_prices' in c['remaining_source_fields']


def test_old_pdf_new_transcription_is_not_backdated():
    d = document()
    d['archive_id'] = 'old'
    r = fields._record(d,'digest','2026-05-14T08:00:00+00:00',CREATED)
    assert r['source_received_at'].startswith('2026-05-14')
    assert r['structured_available_at'] == CREATED
    assert stage([r])['historically_available_new_documents'] == 0


def test_future_documents_are_excluded_and_do_not_fill_fields():
    r = stage([record(at='2026-09-11T08:00:00+00:00')])
    assert r['mapped_fields_pending_review'] == 0
    assert r['cases'][0]['excluded_future_document_ids'] == ['fixture']


def test_conflicting_dates_remain_unresolved_not_source_order_picked():
    first,second = document(),document()
    second['id'] = 'second'
    second['fields']['record_date']['value'] = '2026-05-13'
    c = stage([record(first),record(second)])['cases'][0]
    assert c['conflicting_fields'] == ['record_date']
    assert 'record_date' not in c['nonconflicting_disclosure_values']
    assert 'record_date' in c['remaining_source_fields']


def test_same_decimal_with_different_scale_not_conflict():
    d = document()
    d['id'] = 'second'
    d['fields']['gross_cny_per_share']['value'] = '0.57000'
    assert stage([record(),record(d)])['cases'][0]['conflicting_fields'] == []


def test_native_gross_conflict_not_overwritten_by_disclosure():
    c = stage(actions=[native('0.056')])['cases'][0]
    assert c['native_cash_conflict_evidence_ids'] == ['native']
    assert 'resolve_disclosure_or_native_component_conflict' in c['remaining_source_fields']


@pytest.mark.parametrize('key,value', [('record_date','2026-05-15'),('cash_pay_date','2026-05-14'),
    ('gross_cny_per_share',False),('gross_cny_per_share','NaN'),('gross_cny_per_share','-1'),
    ('bonus_shares_per_share','101'),('record_date','2026-5-14'),('ex_date','2026-05-16')])
def test_invalid_values_rejected(key,value):
    d = document()
    d['fields'][key]['value'] = value
    with pytest.raises(ValueError):
        fields.validate_document(d)


@pytest.mark.parametrize('pages', [[],[0],[True],[1,1]])
def test_known_value_requires_valid_distinct_one_based_pages(pages):
    d = document()
    d['fields']['record_date']['pages'] = pages
    with pytest.raises(ValueError):
        fields.validate_document(d)


@pytest.mark.parametrize('url', ['http://static.cninfo.com.cn/a.PDF','https://example.com/a.PDF',
    'https://static.cninfo.com.cn/a.PDF?token=x'])
def test_non_public_or_non_allowlisted_source_rejected(url):
    d = document()
    d['url'] = url
    with pytest.raises(ValueError):
        fields.validate_document(d)


def test_wrong_case_and_future_replay_rejected():
    d = document()
    d['instrument'] = 'SH.600000'
    with pytest.raises(ValueError,match='scope'):
        stage([record(d)])
    with pytest.raises(ValueError,match='cutoff'):
        stage(replay_asof='2027-01-01T00:00:00+00:00')


def fake_parents(monkeypatch, tmp_path, doc=None):
    d = doc or document()
    plan = {'scope':fields.SCOPE,'adapter':str(tmp_path/'parent'),'documents':[d]}
    archived = {'fixture':(PDF,'2026-05-14T08:00:00+00:00')} if d['archive_id'] else {}
    monkeypatch.setattr(fields,'_parents',lambda _: (parent(d['kind']),[],archived,'binding'))
    return plan


def reseal(folder):
    manifest,_ = read_json(folder/'completed.json')
    manifest.pop('manifest_id')
    manifest['artifact_hashes'] = {p.name:file_hash(p) for p in folder.iterdir() if p.name != 'completed.json'}
    (folder/'completed.json').write_text(canonical({**manifest,'manifest_id':identity(manifest)}),encoding='utf-8')


def test_package_roundtrip_and_new_directory(monkeypatch,tmp_path):
    plan = fake_parents(monkeypatch,tmp_path)
    out = tmp_path/'new'
    before = deepcopy(plan)
    result = fields.build_fields(plan,out,fetch=lambda _:PDF,clock=lambda:CREATED)
    assert fields.verify_fields(out) == result and plan == before
    assert (out/'fixture.pdf').read_bytes() == PDF
    with pytest.raises(FileExistsError):
        fields.build_fields(plan,out)


@pytest.mark.parametrize('member,mutation', [
    ('report.json',lambda r:r.update(execution_ready=True)),
    ('records.json',lambda r:r['records'][0].update(independent_review='approved')),
    ('records.json',lambda r:r['records'][0].update(structured_available_at=PAST))])
def test_resealed_report_or_authority_tampering_rejected(monkeypatch,tmp_path,member,mutation):
    plan = fake_parents(monkeypatch,tmp_path)
    out = tmp_path/'new'
    fields.build_fields(plan,out,fetch=lambda _:PDF,clock=lambda:CREATED)
    value,_ = read_json(out/member)
    mutation(value)
    (out/member).write_text(canonical(value),encoding='utf-8')
    reseal(out)
    with pytest.raises(ValueError):
        fields.verify_fields(out)


def test_archived_pdf_copied_without_fetch_or_knowledge_backdating(monkeypatch,tmp_path):
    d = document()
    d['archive_id'] = 'old'
    plan = fake_parents(monkeypatch,tmp_path,d)
    out = tmp_path/'new'
    def no_fetch(_):
        raise AssertionError('must not fetch archived source')
    fields.build_fields(plan,out,fetch=no_fetch,clock=lambda:CREATED)
    assert fields.verify_fields(out)['historically_available_new_documents'] == 0
    (out/'fixture.pdf').write_bytes(b'%PDF-changed')
    reseal(out)
    with pytest.raises(ValueError,match='archived source bytes'):
        fields.verify_fields(out)


def test_failed_fetch_does_not_seal_or_retry(monkeypatch,tmp_path):
    plan = fake_parents(monkeypatch,tmp_path)
    calls = []
    def fail(url):
        calls.append(url)
        raise ValueError('HTTP 429')
    out = tmp_path/'new'
    with pytest.raises(ValueError):
        fields.build_fields(plan,out,fetch=fail,clock=lambda:CREATED)
    assert len(calls) == 1 and not (out/'completed.json').exists()


def test_changed_parent_prevents_completion(monkeypatch,tmp_path):
    plan = fake_parents(monkeypatch,tmp_path)
    calls = []
    def changed(_):
        calls.append(1)
        return parent(),[],{},'binding' if len(calls) == 1 else 'changed'
    monkeypatch.setattr(fields,'_parents',changed)
    out = tmp_path/'new'
    with pytest.raises(ValueError,match='parent changed'):
        fields.build_fields(plan,out,fetch=lambda _:PDF,clock=lambda:CREATED)
    assert not (out/'completed.json').exists()


def test_extra_resealed_member_rejected(monkeypatch,tmp_path):
    plan = fake_parents(monkeypatch,tmp_path)
    out = tmp_path/'new'
    fields.build_fields(plan,out,fetch=lambda _:PDF,clock=lambda:CREATED)
    (out/'extra.txt').write_text('unexpected')
    reseal(out)
    with pytest.raises(ValueError,match='unexpected sealed member'):
        fields.verify_fields(out)


def test_parent_loader_validates_kind_archive_identity_and_budget(monkeypatch,tmp_path):
    p = tmp_path/'parent'
    p.mkdir()
    write_json(p/'registration.json',{'plan':{'supplement':str(tmp_path/'supplement')}})
    write_json(p/'action_candidates.json',{'rows':[]})
    monkeypatch.setattr(fields,'verify_adapter',lambda _:parent())
    plan = {'scope':fields.SCOPE,'adapter':str(p),'documents':[document('halt')]}
    with pytest.raises(ValueError,match='case kind'):
        fields._parents(plan)
    plan['documents'] = [{**document(),'id':str(i)} for i in range(8)]
    with pytest.raises(ValueError,match='seven'):
        fields._parents(plan)
