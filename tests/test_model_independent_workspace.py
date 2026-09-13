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
    with pytest.raises(ValueError,match='market evidence changed'):
        product.save_note(tmp_path,dict(command(),request_id='4'*32,evidence_id='0'*64))


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
