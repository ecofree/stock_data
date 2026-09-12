"""Synthetic acceptance fixtures, never real human judgement or market evidence."""
import http.client
import json
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from trade_system.v2 import research_product as product, research_product_server as server
from trade_system.v2 import research_journal as journal
from trade_system.v2.gap_evidence import read_json
from tests.test_research_delivery import prediction


def command():
    return dict(prediction_id=prediction()['prediction_id'],instrument='000001',
                operator='SYNTHETIC ACCEPTANCE ONLY',intent='observe',
                hypothesis='synthetic thesis, not advice',invalidation='synthetic condition',
                request_id='a'*32)


def test_retry_returns_original_event_even_after_prediction_moves(tmp_path,monkeypatch):
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    first=product.save_note(tmp_path,command())
    before=(tmp_path/'notes'/(first+'.json')).read_bytes()
    monkeypatch.setattr(product,'read_prediction',lambda _:None)
    assert product.save_note(tmp_path,command())==first
    assert (tmp_path/'notes'/(first+'.json')).read_bytes()==before
    assert len(list((tmp_path/'notes').glob('*.json')))==1


def test_request_cannot_be_reused_with_changed_content(tmp_path,monkeypatch):
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    product.save_note(tmp_path,command())
    with pytest.raises(ValueError,match='different judgement'):
        product.save_note(tmp_path,dict(command(),hypothesis='changed'))
    assert len(list((tmp_path/'notes').glob('*.json')))==1


def test_new_judgement_keeps_full_original_queue_without_future_evaluation(tmp_path,monkeypatch):
    p=prediction()
    monkeypatch.setattr(product,'read_prediction',lambda _:p)
    product.save_note(tmp_path,command())
    data=product.journal_projection(tmp_path,{'prediction':p,'reviews':[]})
    review=data['reviews'][0]
    assert review['status']=='awaiting_review_refresh' and review['paired']==0
    assert len(review['rows'])==len(p['rows']) and not review['count_in_summary']
    assert all(r['target_pct'] is None for r in review['rows'])
    assert review['rows'][0]['judgements'][0]['operator']=='SYNTHETIC ACCEPTANCE ONLY'
    assert len(product.journal_projection(tmp_path,data)['reviews'])==1


@pytest.mark.parametrize('request_id',['../bad','a'*31,'z'*32,True])
def test_invalid_request_id_rejected_before_prediction(tmp_path,monkeypatch,request_id):
    monkeypatch.setattr(product,'read_prediction',lambda _:pytest.fail('must reject first'))
    with pytest.raises(ValueError,match='request id'):
        product.save_note(tmp_path,dict(command(),request_id=request_id))


def test_partial_write_never_becomes_a_judgement(tmp_path,monkeypatch):
    import os
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    original=os.replace
    def fail(*args):raise OSError('synthetic rename failure')
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError):product.save_note(tmp_path,command())
    assert list((tmp_path/'notes').glob('*.json'))==[]
    assert list((tmp_path/'notes').glob('.pending-*'))
    monkeypatch.setattr(os,'replace',original)
    note_id=product.save_note(tmp_path,command())
    assert journal.read_note(tmp_path,note_id)['request_id']=='a'*32


def test_revision_retry_does_not_append_another_version(tmp_path,monkeypatch):
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    first=product.save_note(tmp_path,command())
    changed=dict(command(),request_id='b'*32,hypothesis='synthetic revision',supersedes=first)
    second=product.save_note(tmp_path,changed)
    assert product.save_note(tmp_path,changed)==second
    notes=journal.annotate([read_json(p)[0] for p in (tmp_path/'notes').glob('*.json')])
    assert len(notes)==2 and sum(n['is_latest'] for n in notes)==1


def test_http_acknowledgement_survives_broken_workspace_renderer(tmp_path,monkeypatch):
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    def fail(*args):raise RuntimeError('synthetic broken renderer')
    monkeypatch.setattr(server,'read_current',fail)
    service=ThreadingHTTPServer(('127.0.0.1',0),server.handler(tmp_path,tmp_path,0))
    port=service.server_port
    service.RequestHandlerClass=server.handler(tmp_path,tmp_path,port)
    thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
    def request(path,values=None):
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        headers={'Origin':f'http://127.0.0.1:{port}','Content-Type':'application/x-www-form-urlencoded'}
        conn.request('POST' if values else 'GET',path,urlencode(values) if values else None,headers)
        response=conn.getresponse();result=(response.status,response.getheader('Location'),response.read().decode())
        conn.close();return result
    try:
        health=json.loads(request('/health')[2])
        assert health['source_matches'] and health['workspace_id']==server.service_identity(tmp_path,tmp_path)['workspace_id']
        form=dict(command(),csrf=service.RequestHandlerClass.csrf_token)
        status,location,_=request('/note',form)
        assert status==303 and location.startswith('/receipt/')
        assert request('/note',form)[1]==location
        assert request('/')[0]==503
        status,_,body=request(location)
        assert status==200 and '判断已保存' in body and 'synthetic thesis' in body
        assert '/#review-'+prediction()['prediction_id'] in body
        assert len(list((tmp_path/'notes').glob('*.json')))==1
        assert request('/receipt/../../other')[0]==404
        note_id=location.rsplit('/',1)[1]
        review_form=dict(csrf=form['csrf'],request_id='b'*32,note_id=note_id,
                         conclusion='pending',reviewer='SYNTHETIC REVIEWER ONLY',evidence='synthetic pending observation')
        status,review_location,_=request('/review',review_form)
        assert status==303 and review_location.startswith('/review-receipt/')
        assert request('/review',review_form)[1]==review_location
        assert '复盘已保存' in request(review_location)[2]
        assert len(list((tmp_path/'notes/reviews').glob('*.json')))==1
        missing=dict(form);missing.pop('request_id')
        assert request('/note',missing)[0]==400
    finally:service.shutdown();service.server_close();thread.join()


def test_service_identity_distinguishes_workspace_and_changed_source(tmp_path,monkeypatch):
    a=server.service_identity(tmp_path,tmp_path/'a')
    b=server.service_identity(tmp_path,tmp_path/'b')
    assert a['workspace_id']!=b['workspace_id'] and a['surface_sha256']==b['surface_sha256']
    from trade_system.v2 import domain
    monkeypatch.setattr(domain,'file_hash',lambda _: 'changed')
    assert server.service_identity(tmp_path,tmp_path/'a')['surface_sha256']!=a['surface_sha256']


def test_launcher_does_not_reuse_generic_legacy_header_or_bypass_policy():
    root=Path(__file__).resolve().parents[1]
    source=(root/'scripts/start_research_workbench.ps1').read_text(encoding='utf-8')
    assert 'local-v3' not in source and 'surface_sha256' in source and 'workspace_id' in source
    assert '-WindowStyle Hidden' in source and '[int]$Port=8769' in source
    assert 'ExecutionPolicy Bypass' not in (root/'Start Research.cmd').read_text(encoding='utf-8')


def test_receipt_escapes_human_text_and_clears_only_matching_draft(tmp_path,monkeypatch):
    from trade_system.v2.research_product_view import receipt_page
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    note_id=product.save_note(tmp_path,dict(command(),hypothesis='<script>alert(1)</script>'))
    html=receipt_page(journal.read_note(tmp_path,note_id))
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html and 'JSON.parse(raw).request_id===' in html


def test_human_condition_review_is_append_only_and_never_a_price_label(tmp_path,monkeypatch):
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    note_id=product.save_note(tmp_path,command())
    note_bytes=(tmp_path/'notes'/(note_id+'.json')).read_bytes()
    values=dict(note_id=note_id,request_id='d'*32,conclusion='pending',
                reviewer='SYNTHETIC ONLY',evidence='waiting, not a real future observation')
    first=journal.save_review(tmp_path,values)
    assert journal.save_review(tmp_path,values)==first
    with pytest.raises(ValueError,match='different content'):
        journal.save_review(tmp_path,dict(values,conclusion='triggered'))
    second=journal.save_review(tmp_path,dict(values,request_id='e'*32,conclusion='unclear'))
    assert first!=second and len(list((tmp_path/'notes/reviews').glob('*.json')))==2
    review=journal.read_review(tmp_path,first)
    assert review['scope']=='human_condition_observation_not_price_label_or_account_acceptance'
    assert review['execution_ready'] is False
    assert (tmp_path/'notes'/(note_id+'.json')).read_bytes()==note_bytes
    data=product.journal_projection(tmp_path,dict(prediction=prediction(),reviews=[]))
    assert len(data['human_reviews'])==2 and data['reviews'][0]['paired']==0


def test_review_requires_existing_parent_and_explicit_evidence(tmp_path,monkeypatch):
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    note_id=product.save_note(tmp_path,command())
    values=dict(note_id=note_id,request_id='d'*32,conclusion='pending',reviewer='SYNTHETIC',evidence='')
    with pytest.raises(ValueError,match='human review'):journal.save_review(tmp_path,values)
    with pytest.raises(FileNotFoundError):journal.save_review(tmp_path,dict(values,note_id='f'*64,evidence='test'))
    assert not (tmp_path/'notes/reviews').exists()
