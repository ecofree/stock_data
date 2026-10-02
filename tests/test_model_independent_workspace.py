"""Synthetic no-model contracts. These records never enter the user's workspace."""
import json
import subprocess
import sys

import pytest

from trade_system.v2 import research_product as product, research_journal as journal
from trade_system.v2.domain import identity
from trade_system.v2.gap_evidence import write_json
from trade_system.v2.publisher import read_current


def market():
    value={'trade_date':'2026-09-11','as_of':'2026-09-13T12:00:00+08:00',
        'scope':'read_only_market_review_not_execution','execution_ready':False,
        'stocks':{'000001':{'stock_code':'000001','stock_name':'合成证券','change_pct':1}},
        'themes':[],'breadth':{'rise':1,'fall':0},'regime':'未评级','session_state':'closed'}
    return dict(value,snapshot_id=identity(value))


def command():
    return {'instrument':'000001','operator':'SYNTHETIC TEST ONLY','intent':'observe',
        'hypothesis':'synthetic reason','invalidation':'synthetic condition',
        'request_id':'1'*32,'evidence_id':market()['snapshot_id']}


def test_candidate_header_separates_current_market_from_historical_predictions():
    from trade_system.v2.research_product_view import candidate_scope,render
    from trade_system.v2.daily_workspace import empty_projection
    data=empty_projection();data['market']=market()
    data['prediction']={'date':'2026-09-10','predictions':1,'rows':[{}]}
    text=candidate_scope(data)
    assert '市场事实日 2026-09-11' in text
    assert '历史研究预测日 2026-09-10' in text
    assert '异日预测不参与当前比较或排序' in text
    data['prediction']['date']='2026-09-11'
    assert '历史研究预测日' not in candidate_scope(data)
    data['prediction']=None
    assert '市场事实仍可比较与记录' in candidate_scope(data)
    data['market']['trade_date']='</script><script>alert(1)</script>'
    assert '</script><script>alert(1)</script>' not in render(data)


def test_no_model_publish_judgement_retry_review_and_revision(tmp_path):
    product.publish_desk(tmp_path,market=market())
    note_id=product.save_note(tmp_path,command())
    before=(tmp_path/'notes/attention'/(note_id+'.json')).read_bytes()
    assert product.save_note(tmp_path,command())==note_id
    note=journal.read_note(tmp_path,note_id)
    assert note['prediction_id'] is None and note['evidence_id']==market()['snapshot_id']
    revision=product.save_note(tmp_path,dict(command(),request_id='2'*32,supersedes=note_id,hypothesis='revised'))
    assert revision!=note_id and (tmp_path/'notes/attention'/(note_id+'.json')).read_bytes()==before
    review=journal.save_review(tmp_path,dict(note_id=revision,request_id='3'*32,
        reviewer='SYNTHETIC TEST ONLY',evidence='synthetic subsequent check',conclusion='pending'))
    assert journal.read_review(tmp_path,review)['prediction_id'] is None
    assert len(product.saved_projection(tmp_path)['notes'])==2
    assert list((tmp_path/'notes').glob('*.json'))==[]
    assert list((tmp_path/'notes/reviews').glob('*.json'))==[]


def test_changed_snapshot_and_model_failure_do_not_erase_judgement(tmp_path):
    product.publish_desk(tmp_path,market=market())
    first=product.save_note(tmp_path,command())
    write_json(tmp_path/'research-current.json',{'broken':'test'})
    product.publish_desk(tmp_path)
    _,files=read_current(tmp_path/'publication')
    data=json.loads(files['desk.json'])
    assert data['research_status']=='unavailable' and data['notes'][0]['note_id']==first
    assert data['prediction'] is None and data['model']=={}
    assert data['research_retirement']['status']=='retired'
    assert data['market']['stocks']['000001']['change_pct']==1
    with pytest.raises(ValueError,match='market evidence changed'):
        product.save_note(tmp_path,dict(command(),request_id='4'*32,evidence_id='0'*64))


def _retained_forecast_publication(output):
    """Original historical projection remains readable with its original identity."""
    from trade_system.v2.daily_workspace import empty_projection
    from trade_system.v2.publisher import publish
    from trade_system.v2.domain import canonical
    from trade_system.v2.research_product_view import render
    data=empty_projection()
    data.update(market=market(),prediction={'prediction_id':'retained-fixture',
        'date':'2020-01-01','predictions':1,'rows':[{'instrument':'600000'}]},
        model={'model_id':'retained-model'})
    data['report_id']=identity(data)
    publish(output/'publication','retained-original',{'desk.json':canonical(data).encode(),
        'index.html':render(data).encode()},generation=1)
    return data


def test_base_publication_retires_legacy_forecast_without_changing_original_pointer(tmp_path,monkeypatch):
    write_json(tmp_path/'research-current.json',{'retained':'fixture'})
    pointer=(tmp_path/'research-current.json').read_bytes()
    monkeypatch.setattr(product,'_research_projection',lambda _:dict(
        prediction={'date':'2026-09-11','predictions':1,'rows':[{'instrument':'600000'}]},
        model={'model_id':'retained-model'},candidate_model={'model_id':'candidate'},
        candidate_readiness={'ready':True}))
    product.publish_desk(tmp_path,market=market())
    _,files=read_current(tmp_path/'publication');data=json.loads(files['desk.json'])
    assert data['market']['stocks']['000001']['change_pct']==1
    assert data['prediction'] is None and data['model']=={} and data['candidate_model'] is None
    assert data['candidate_readiness'] is None and not data['research_retirement']['automatic_promotion']
    assert data['retained_prediction_evidence']['rows'][0]['instrument']=='600000'
    assert (tmp_path/'research-current.json').read_bytes()==pointer
    html=files['index.html'].decode()
    assert 'action="/update"' not in html and 'value="qlib"' not in html


def test_observe_preserves_human_scope_without_using_retired_forecasts(tmp_path,monkeypatch):
    from trade_system.v2 import observation_workspace
    original=_retained_forecast_publication(tmp_path)
    write_json(tmp_path/'workspace-config.json',{'market_database':'synthetic-unused','read_only':True})
    selected=[]
    def load(database,codes,as_of):
        selected.append(set(codes));return []
    monkeypatch.setattr(observation_workspace,'load_rows',load)
    assert product.saved_projection(tmp_path)['prediction']==original['prediction']
    note=product.save_note(tmp_path,command())
    result=product.observe(tmp_path)
    assert selected==[{'000001'}] and result['provider_requests']==0 and result['fits']==0
    _,files=read_current(tmp_path/'observation-publication')
    live=json.loads(files['observation.json'])['live_scope']
    assert [row['instrument'] for row in live]==['000001']
    assert 'human_attention' in live[0]['roles'] and 'research_forecast' not in live[0]['roles']
    assert journal.read_note(tmp_path,note)['evidence_id']==market()['snapshot_id']


def test_present_current_market_is_independent_of_old_forecast_but_keeps_snapshot_guard(tmp_path):
    original=_retained_forecast_publication(tmp_path)
    path=tmp_path/'market.json';write_json(path,market())
    with pytest.raises(ValueError,match='dates differ'):
        product.saved_projection(tmp_path,market_review=path)
    product.present(tmp_path,market_review=path)
    _,files=read_current(tmp_path/'publication');data=json.loads(files['desk.json'])
    assert data['prediction'] is None and data['market']['trade_date']=='2026-09-11'
    assert data['retained_prediction_evidence']==original['prediction']
    before=(tmp_path/'publication/current.json').read_bytes()
    broken=market();broken['stocks']['000001']['change_pct']=2
    path.write_text(json.dumps(broken),encoding='utf-8')
    with pytest.raises(ValueError,match='snapshot identity changed'):
        product.present(tmp_path,market_review=path)
    assert (tmp_path/'publication/current.json').read_bytes()==before


def test_unavailable_valuation_and_flow_confirmation_preserve_market_and_notes(tmp_path):
    value=market();value.pop('snapshot_id')
    value['operational_capabilities']={
        'schema':'operational_capabilities_v1','trade_date':value['trade_date'],'as_of':value['as_of'],
        'capabilities':{'market_view':{'ready':True,'qualified_rows':1,'expected_rows':2},
            'price_research':{'ready':False},'flow_observation':{'ready':False},
            'flow_confirmation':{'ready':False}},'execution_ready':False}
    value['valuation_capabilities']={'rows':{'000001.SZ':{'current_core_available':False,
        'reasons':{'pb':['no_qualified_same_session_pb']}}}}
    value['snapshot_id']=identity(value)
    product.publish_desk(tmp_path,market=value)
    note_id=product.save_note(tmp_path,dict(command(),evidence_id=value['snapshot_id']))
    product.publish_desk(tmp_path,market=value)
    _,files=read_current(tmp_path/'publication')
    data=json.loads(files['desk.json'])
    assert data['market']['stocks']['000001']['change_pct']==1
    assert data['market']['operational_capabilities']['capabilities']['flow_confirmation']['ready'] is False
    assert data['market']['valuation_capabilities']['rows']['000001.SZ']['current_core_available'] is False
    assert data['notes'][0]['note_id']==note_id and data['execution_ready'] is False
    html=files['index.html'].decode() if isinstance(files['index.html'],bytes) else files['index.html']
    assert 'capability-state' in html and '逐证券估值缺口与历史参考' in html
    assert 'Object.entries(values)' in html and 'no_qualified_same_session_pb' in html


def test_daily_import_and_render_without_any_research_packages():
    script='''
import importlib.abc, sys
class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('qlib','lightgbm','pandas','numpy'):
            raise ImportError('research package deliberately absent')
sys.meta_path.insert(0,BlockResearch())
from trade_system.v2.research_product_server import handler
from trade_system.v2.daily_workspace import empty_projection
from trade_system.v2.research_product_view import render
assert '每日观察与复盘' in render(empty_projection())
'''
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_independent_receipt_links_to_its_own_followup(tmp_path):
    from trade_system.v2.research_product_view import receipt_page
    product.publish_desk(tmp_path,market=market())
    note_id=product.save_note(tmp_path,command())
    page=receipt_page(journal.read_note(tmp_path,note_id))
    assert '市场证据' in page and '原预测批次' not in page
    assert '/#review-'+note_id in page
