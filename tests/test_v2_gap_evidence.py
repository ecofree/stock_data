import duckdb
import pytest

from trade_system.v2.domain import canonical, identity, utc
from trade_system.v2 import gap_audit as audit
from trade_system.v2 import gap_evidence as evidence


NOW = '2026-09-10T12:00:00+08:00'
CASE = {'case_id': 'fixture-case', 'instrument': 'SZ.000609', 'date': '2026-04-22'}


@pytest.fixture
def make_evidence(tmp_path):
    sequence = []
    def make(kind='session_status', value=None, family='hithink_official', *, receipt_update=None, claim_update=None):
        n = len(sequence)
        sequence.append(n)
        claim = {'instrument': CASE['instrument'], 'date': CASE['date'], 'kind': kind,
                 'value': value or {'status': 'suspended', 'coverage': 'full_session'}}
        claim.update(claim_update or {})
        raw = canonical({'schema_version': 1, 'rows': [claim]}).encode()
        raw_path = tmp_path/f'raw-{n}.json'
        raw_path.write_bytes(raw)
        receipt = {'schema_version': 1, 'source_family': family, 'source_api': 'fixture.status',
                   'origin_family': 'fixture-origin', 'reference': 'synthetic-reference', 'source_revision': 'r1',
                   'source_event_at': '2026-04-22T16:00:00+08:00',
                   'provider_received_at': '2026-04-22T16:01:00+08:00',
                   'license_status': 'unverified', 'row_index': 0, 'raw_sha256': evidence.sha(raw)}
        receipt.update(receipt_update or {})
        receipt_path = tmp_path/f'receipt-{n}.json'
        receipt_path.write_text(canonical(receipt), encoding='utf-8')
        folder = tmp_path/f'import-{n}'
        record = evidence.ingest_response(receipt_path, raw_path, folder, clock=lambda: utc(NOW))
        return record, folder
    return make


def judge(records, case=CASE, asof=NOW, mode='historical_repair'):
    return evidence.adjudicate(case, records, asof=asof, mode=mode)


def raw_bar(close='11.00'):
    return {'open': '10', 'high': '12', 'low': '9', 'close': close,
            'adjustment': 'none', 'currency': 'CNY', 'finality': 'final'}


def test_import_binds_original_bytes_and_local_clock(make_evidence):
    record, folder = make_evidence()
    assert evidence.read_evidence(folder) == record
    assert record['available_at'] == utc(NOW).isoformat()
    assert record['source_authenticity'] == 'not_independently_verified'
    r = judge([record])
    assert r['classification'] == 'suspension_supported_not_authenticated'
    assert not any(r[k] for k in ('execution_ready', 'can_apply_to_account', 'can_resume_portfolio', 'can_forward_fill_mark'))


def test_provider_historical_time_cannot_backdate_local_knowledge(make_evidence):
    record, _ = make_evidence()
    r = judge([record], asof='2026-04-22T18:00:00+08:00', mode='system_replay')
    assert r['classification'] == 'unknown' and r['excluded_evidence'][0]['reason'] == 'not_yet_known'
    assert judge([record])['classification'].startswith('suspension_supported')


@pytest.mark.parametrize('update', [{'raw_sha256': '0'*64}, {'row_index': True}, {'row_index': 3},
    {'source_event_at': '2026-04-23T00:00:00+08:00'}, {'provider_received_at': '2026-09-11T00:00:00+08:00'},
    {'source_event_at': '2026-04-22T00:00:00'}, {'source_revision': ''}, {'source_api': ''},
    {'source_family': 'guessed'}, {'license_status': 'certified'}, {'available_at': '2020-01-01'}])
def test_receipt_cannot_forge_identity_times_or_authority(make_evidence, update):
    with pytest.raises(ValueError):
        make_evidence(receipt_update=update)


@pytest.mark.parametrize('change', [{'close': '99'}, {'close': 'NaN'}, {'open': '-1'}, {'close': '1.123'},
    {'adjustment': 'qfq'}, {'currency': 'USD'}, {'finality': 'provisional'}])
def test_invalid_raw_price_candidate_rejected(make_evidence, change):
    with pytest.raises(ValueError):
        make_evidence('daily_bar', {**raw_bar(), **change})


def test_final_bar_cannot_precede_session_close(make_evidence):
    with pytest.raises(ValueError, match='session close'):
        make_evidence('daily_bar', raw_bar(), receipt_update={'source_event_at': '2026-04-22T09:00:00+08:00'})


def test_priority_does_not_hide_contradictions(make_evidence):
    official, _ = make_evidence()
    other, _ = make_evidence('daily_bar', raw_bar(), 'other')
    r = judge([other, official])
    assert r == judge([official, other])
    assert r['classification'] == 'conflicting_evidence' and r['candidate_raw_bar'] is None
    assert 'full_session_suspension_vs_traded' in r['conflicts']


def test_equal_quotes_choose_declared_priority_but_not_independent_proof(make_evidence):
    other, _ = make_evidence('daily_bar', raw_bar(), 'other')
    official, _ = make_evidence('daily_bar', raw_bar(), 'hithink_official')
    r = judge([other, official, official])
    assert r['classification'] == 'raw_bar_candidate_not_authenticated'
    assert r['preferred_evidence_id'] == official['evidence_id']
    assert len(r['included_evidence_ids']) == 1 and r['independently_verified_origins'] == 0


def test_disagreeing_raw_bars_and_snapshot_cannot_be_silently_replaced(make_evidence):
    a, _ = make_evidence('daily_bar', raw_bar())
    b, _ = make_evidence('daily_bar', raw_bar('10.5'), 'xiaodefa_tushare')
    assert judge([a, b])['classification'] == 'conflicting_evidence'
    case = {**CASE, 'observations': {'tushare_daily': {'rows': [raw_bar('10.5')]}}}
    assert judge([a], case)['conflicts'] == ['candidate_vs_snapshot_raw_price']


def test_snapshot_price_contradicts_full_suspension(make_evidence):
    r, _ = make_evidence()
    case = {**CASE, 'observations': {'tushare_daily': {'rows': [raw_bar()]}}}
    assert judge([r], case)['classification'] == 'conflicting_evidence'


@pytest.mark.parametrize('kind,value,expected', [
    ('session_status', {'status': 'suspended', 'coverage': 'intraday'}, 'unknown'),
    ('collector_status', {'outcome': 'successful_empty', 'request_id': 'fixture'}, 'unknown'),
    ('collector_status', {'outcome': 'complete', 'request_id': 'fixture'}, 'unknown'),
    ('collector_status', {'outcome': 'failed', 'request_id': 'fixture'}, 'collection_failure_supported_cause_unresolved'),
    ('lifecycle', {'status': 'delisted', 'effective_from': '2026-04-01', 'effective_to': '2026-04-30'}, 'lifecycle_supported_not_authenticated')])
def test_absence_and_partial_halt_do_not_prove_full_suspension(make_evidence, kind, value, expected):
    r, _ = make_evidence(kind, value)
    assert judge([r])['classification'] == expected


def test_lifecycle_scope_and_session_conflict(make_evidence):
    with pytest.raises(ValueError):
        make_evidence('lifecycle', {'status': 'delisted', 'effective_from': '2026-05-01', 'effective_to': '2026-06-01'})
    life, _ = make_evidence('lifecycle', {'status': 'delisted', 'effective_from': '2026-04-01', 'effective_to': '2026-04-30'})
    suspension, _ = make_evidence()
    assert judge([life, suspension])['classification'] == 'conflicting_evidence'


def test_corporate_action_requires_entitlement_and_no_duplicate_application(make_evidence):
    value = {'action_type': 'cash_dividend', 'ex_date': CASE['date'], 'action_id': 'fixture', 'gross_cny_per_share': '.2'}
    one, _ = make_evidence('corporate_action', value)
    two, _ = make_evidence('corporate_action', value, 'other')
    r = judge([one, two])
    assert r['classification'] == 'corporate_action_candidate_not_authenticated'
    assert len(r['candidate_corporate_actions']) == 1 and not r['can_apply_to_account']
    three, _ = make_evidence('corporate_action', {**value, 'gross_cny_per_share': '.3'})
    assert judge([one, three])['classification'] == 'conflicting_evidence'


def test_wrong_case_and_restricted_license_excluded(make_evidence):
    other, _ = make_evidence(claim_update={'instrument': 'SZ.000001'})
    restricted, _ = make_evidence(receipt_update={'license_status': 'restricted'})
    r = judge([other, restricted])
    assert r['classification'] == 'unknown' and len(r['excluded_evidence']) == 2


@pytest.mark.parametrize('file', ['response.json', 'evidence.json', 'extra.txt'])
def test_import_tamper_or_member_addition_rejected(make_evidence, file):
    _, folder = make_evidence()
    (folder/file).write_text('{}')
    with pytest.raises(ValueError):
        evidence.read_evidence(folder)


def test_raw_to_claim_binding_not_just_manifest(make_evidence):
    r, folder = make_evidence()
    r['claim']['value']['status'] = 'traded'
    r.pop('evidence_id')
    r['evidence_id'] = identity(r)
    (folder/'evidence.json').write_text(canonical(r))
    with pytest.raises(ValueError, match='response row'):
        evidence.read_evidence(folder)


def test_duplicate_json_key_and_size_limit(tmp_path, monkeypatch):
    p = tmp_path/'bad.json'
    p.write_text('{"x":1,"x":2}')
    with pytest.raises(ValueError, match='duplicate'):
        evidence.read_json(p)
    monkeypatch.setattr(evidence, 'MAX_BYTES', 3)
    with pytest.raises(ValueError, match='size'):
        evidence.read_json(p)


@pytest.fixture
def case_audit(tmp_path):
    p = tmp_path/'audit'
    p.mkdir()
    evidence.write_json(p/'report.json', {'cases': [CASE]})
    audit.seal(p, {'source': 'synthetic'})
    return p


def test_adjudication_archives_evidence_and_rejects_overwrite(case_audit, make_evidence, tmp_path):
    record, folder = make_evidence()
    out = tmp_path/'decision'
    result = audit.adjudicate_audit(case_audit, [folder], out, clock=lambda: utc(NOW))
    assert result == audit.read_package(out)
    assert evidence.read_evidence(out/'evidence'/record['evidence_id']) == record
    assert not result['portfolio_resumed']
    with pytest.raises(FileExistsError):
        audit.adjudicate_audit(case_audit, [], out, clock=lambda: utc(NOW))
    (out/'evidence'/record['evidence_id']/'response.json').write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        audit.read_package(out)


def test_empty_evidence_unknown_and_future_cutoff_rejected(case_audit, tmp_path):
    r = audit.adjudicate_audit(case_audit, [], tmp_path/'empty', clock=lambda: utc(NOW))
    assert r['classification_counts'] == {'unknown': 1}
    with pytest.raises(ValueError, match='future'):
        audit.adjudicate_audit(case_audit, [], tmp_path/'future', asof='2026-09-11T00:00:00+08:00', clock=lambda: utc(NOW))


def test_package_code_and_content_tampering(case_audit, monkeypatch):
    monkeypatch.setattr(audit, 'implementation', lambda: {})
    with pytest.raises(ValueError, match='implementation'):
        audit.read_package(case_audit)


def test_snapshot_audit_separates_bar_from_factor(tmp_path, monkeypatch):
    parent = tmp_path/'portfolio'
    (parent/'run').mkdir(parents=True)
    (parent/'run/completed.json').write_text('{}')
    source = tmp_path/'source.duckdb'
    with duckdb.connect(str(source)) as c:
        c.execute("CREATE TABLE tushare_daily(ts_code VARCHAR,date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,adjustment VARCHAR,provider VARCHAR,fetched_at TIMESTAMP)")
        c.execute("CREATE TABLE tushare_adj_factor AS SELECT '000609.SZ' ts_code,'000609' stock_code,DATE '2026-04-22' date,1.0 adj_factor,TIMESTAMP '2026-09-10 00:00:00' fetched_at")
    policy = {'top_k': 1, 'hold_sessions': 2}
    bundle = {'calendar': ['2026-04-21', '2026-04-22', '2026-04-23'], 'bars': [],
              'predictions': {'price': [['2026-04-21', '000609', 1]]}, 'identity_map': {'000609': 'SZ.000609'}}
    (parent/'registration.json').write_text(canonical({'policy': policy, 'snapshot_sha256': audit.file_hash(source)}))
    (parent/'inputs.json').write_text(canonical(bundle))
    monkeypatch.setattr(audit, 'read_portfolio_result', lambda _: {'registration_id': 'fixture', 'accounts': {}})
    out = tmp_path/'audit'
    r = audit.audit_portfolio(parent, source, out, clock=lambda: utc(NOW))
    assert r == audit.read_package(out) and len(r['cases']) == 2
    assert r['cases'][0]['source_diagnosis']['distinction'] == 'raw_bar_missing_factor_exists'
    assert r['cases'][1]['source_diagnosis']['distinction'] == 'both_raw_bar_and_factor_missing'
    assert not r['cases'][0]['observations']['kline']['table_present']
    assert r['classification_counts'] == {'unknown': 2}
    with pytest.raises(FileExistsError):
        audit.audit_portfolio(parent, source, out)


def test_potential_windows_do_not_drop_post_blocker_dates():
    bundle = {'calendar': ['2026-04-21', '2026-04-22', '2026-04-23', '2026-04-24'],
              'predictions': {'price': [['2026-04-21', '000609', 1], ['2026-04-22', '000609', 2]]},
              'identity_map': {'000609': 'SZ.000609'}}
    windows = audit.potential_windows(bundle, {'top_k': 1, 'hold_sessions': 2})
    assert len(windows) == 2 and windows[-1]['days'][-1] == '2026-04-24'
