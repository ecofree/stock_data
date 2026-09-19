from copy import deepcopy
from pathlib import Path

import pytest

from trade_system.v2 import case_supplement as source
from trade_system.v2.domain import identity
from trade_system.v2.gap_audit import seal
from trade_system.v2.gap_evidence import adjudicate, read_evidence, read_json, write_json


NOW = '2026-09-10T15:00:00+08:00'


def document():
    return {'id': 'fixture', 'url': 'https://static.cninfo.com.cn/fixture.PDF', 'title': 'Synthetic source',
        'published_date': '2026-05-07', 'pages': [1], 'instrument': 'SZ.000609', 'date': '2026-04-22',
        'kind': 'session_status', 'value': {'status': 'suspended', 'coverage': 'full_session'},
        'interpretation': 'Synthetic manual reading, no actual source certification', 'method': source.METHOD}


def plan(tmp_path, monkeypatch):
    audit = tmp_path/'audit'
    audit.mkdir()
    write_json(audit/'report.json', {'cases': [{'case_id': 'fixture-case', 'instrument': 'SZ.000609', 'date': '2026-04-22'}]})
    seal(audit, {'synthetic': True})
    parent = tmp_path/'native'
    (parent/'adjudication/evidence').mkdir(parents=True)
    write_json(parent/'completed.json', {'synthetic_native_parent': True})
    monkeypatch.setattr(source, 'verify_collection', lambda folder: {'scope': 'fixture'})
    return {'scope': source.SCOPE, 'audit': str(audit), 'native_parent': str(parent), 'documents': [document()]}


@pytest.mark.parametrize('url', ['http://static.cninfo.com.cn/a.pdf', 'https://evil.example/a.pdf',
    'https://static.cninfo.com.cn.evil.example/a.pdf', 'https://u:p@static.cninfo.com.cn/a.pdf',
    'https://static.cninfo.com.cn:8080/a.pdf', 'https://static.cninfo.com.cn/a.pdf?token=x',
    'https://static.cninfo.com.cn/a.pdf#x', 'https://static.cninfo.com.cn/a.json'])
def test_exact_public_pdf_allowlist(url):
    doc = document()
    doc['url'] = url
    with pytest.raises(ValueError):
        source.validate_document(doc)


@pytest.mark.parametrize('field,value', [('id','../escape'),('method','automatic_certified'),('pages',[True]),
    ('pages',[0]),('pages',[]),('date','20260422'),('kind','daily_bar'),('value',{'status':'traded'}),
    ('interpretation','')])
def test_strict_manual_document_contract(field, value):
    doc = document()
    doc[field] = value
    with pytest.raises(ValueError):
        source.validate_document(doc)


def test_real_plan_semantics_are_valid_but_not_account_receipts():
    configured, _ = read_json(Path(__file__).resolve().parents[1]/'docs/v2/announcement_supplement_20260910.json')
    for doc in configured['documents']:
        source.validate_document(doc)
    terms = configured['documents'][-1]['value']
    assert terms['action_type'] == 'capitalization' and terms['shares_per_share'] == '0.4'
    assert 'available_at' not in terms and 'tax_rate' not in terms


def test_archive_derivation_time_gate_and_tamper(tmp_path, monkeypatch):
    configured = plan(tmp_path, monkeypatch)
    calls = []
    def fetch(url):
        calls.append(url)
        return b'%PDF-1.4 synthetic fixture, not a parsed real document'
    folder = tmp_path/'run'
    report = source.collect_supplement(configured, folder, fetch=fetch, clock=lambda: NOW)
    assert calls == [document()['url']]
    assert report['classification_counts'] == {'suspension_supported_not_authenticated': 1}
    assert source.verify_supplement(folder) == report
    imported = read_evidence(folder/'fixture/evidence')
    assert imported['available_at'] == '2026-09-10T07:00:00+00:00'
    old = adjudicate({'case_id':'case','instrument':'SZ.000609','date':'2026-04-22'}, [imported],
                     asof='2026-04-22T18:00:00+08:00', mode='system_replay')
    assert old['classification'] == 'unknown' and not old['can_forward_fill_mark']
    with pytest.raises(FileExistsError):
        source.collect_supplement(configured, folder, fetch=fetch, clock=lambda: NOW)
    assert len(calls) == 1
    (folder/'fixture/document.pdf').write_bytes(b'%PDF-1.4 altered')
    with pytest.raises(ValueError):
        source.verify_supplement(folder)


@pytest.mark.parametrize('raw', [b'not PDF', b'%PDF-'+b'x'*source.MAX_BYTES], ids=['not-pdf','oversized-pdf'])
def test_bad_response_never_becomes_empty_evidence(tmp_path, monkeypatch, raw):
    configured = plan(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        source.collect_supplement(configured, tmp_path/'run', fetch=lambda url: raw, clock=lambda: NOW)
    assert (tmp_path/'run/failed.json').exists()
    assert not (tmp_path/'run/completed.json').exists()


def test_scope_budget_checked_before_network(tmp_path, monkeypatch):
    configured = plan(tmp_path, monkeypatch)
    called = []
    configured['documents'] *= 5
    with pytest.raises(ValueError):
        source.collect_supplement(configured, tmp_path/'run', fetch=lambda url: called.append(url))
    assert not called and not (tmp_path/'run').exists()


def test_terms_archive_never_mints_account_receipt(tmp_path, monkeypatch):
    configured = plan(tmp_path, monkeypatch)
    real, _ = read_json(Path(__file__).resolve().parents[1]/'docs/v2/announcement_supplement_20260910.json')
    doc = deepcopy(real['documents'][-1])
    doc['instrument'] = 'SZ.000609'
    doc['date'] = '2026-04-22'
    doc['value'].update(instrument='SZ.000609', record_date='2026-04-21', ex_date='2026-04-22')
    configured['documents'] = [doc]
    result = source.collect_supplement(configured, tmp_path/'run', fetch=lambda url: b'%PDF-1.4 fixture', clock=lambda: NOW)
    assert result['classification_counts'] == {'unknown': 1}
    assert result['documents'][0]['terms']['available_at'] == '2026-09-10T07:00:00+00:00'
    assert result['documents'][0]['account_blockers'] and not result['execution_ready']
    assert source.verify_supplement(tmp_path/'run') == result


def test_semantic_recheck_rejects_escalated_authentication_even_if_resealed(tmp_path, monkeypatch):
    configured = plan(tmp_path, monkeypatch)
    folder = tmp_path/'run'
    source.collect_supplement(configured, folder, fetch=lambda url: b'%PDF-1.4 fixture', clock=lambda: NOW)
    record, _ = read_json(folder/'fixture/record.json')
    record['extraction_is_automatically_verified'] = True
    (folder/'fixture/record.json').write_text(__import__('json').dumps(record), encoding='utf-8')
    manifest, _ = read_json(folder/'completed.json')
    manifest.pop('manifest_id')
    manifest['artifact_hashes']['fixture/record.json'] = source.file_hash(folder/'fixture/record.json')
    (folder/'completed.json').write_text(__import__('json').dumps({**manifest,'manifest_id':identity(manifest)}),encoding='utf-8')
    with pytest.raises(ValueError, match='transcription cannot certify'):
        source.verify_supplement(folder)


@pytest.mark.parametrize('body', [b'%PDF-1.4 synthetic', b'<html>not a PDF</html>'])
def test_current_public_pdf_fetch_uses_bounded_transport(monkeypatch, body):
    from trade_system import http_transport as transport
    calls=[]
    def read(request, **limits):
        calls.append((request,limits))
        return body
    monkeypatch.setattr(transport,'read_verified_once',read)
    if body.startswith(b'%PDF-'):
        assert transport.read_public_pdf(document()['url'])==body
    else:
        with pytest.raises(ValueError,match='not a PDF'):transport.read_public_pdf(document()['url'])
    assert len(calls)==1 and calls[0][1]=={'timeout':15,'max_bytes':4*1024*1024}
    assert not calls[0][0].has_header('Authorization')


@pytest.mark.parametrize('module,command,entry', [('run_case_supplement','collect','collect_supplement'),
                                                 ('run_disclosure_fields','build','build_fields')])
def test_current_historical_cli_injects_shared_transport_without_changing_frozen_readers(tmp_path, monkeypatch, module, command, entry):
    import runpy
    import sys
    from trade_system.v2 import case_supplement, disclosure_fields
    from trade_system.http_transport import read_public_pdf, request_deadline
    plan_path=tmp_path/'plan.json';write_json(plan_path,{'synthetic': True})
    calls=[]
    def collect(plan,output,*,fetch):
        assert fetch is read_public_pdf and request_deadline.get() is not None
        calls.append((plan,output))
        keys=('scope','public_pdf_requests','classification_counts','execution_ready') if entry=='collect_supplement' else (
            'case_count','potential_windows','new_documents','document_kind_counts','closed_halt_candidates',
            'mapped_fields_pending_review','cases_with_conflicts','historically_available_new_documents','emitted_parent_events','execution_ready')
        return dict.fromkeys(keys,False)
    monkeypatch.setattr(case_supplement if entry=='collect_supplement' else disclosure_fields,entry,collect)
    monkeypatch.setattr(sys,'argv',[module,command,'--plan',str(plan_path),'--output',str(tmp_path/'unused')])
    runpy.run_module('tools.v2.'+module,run_name='__main__')
    assert len(calls)==1 and request_deadline.get() is None
