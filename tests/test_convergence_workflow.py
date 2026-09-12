"""Convergence contracts: synthetic tests are never real operator evidence."""
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest

from trade_system.file_lock import FileLock, FileLockBusy
from trade_system.v2 import research_campaign as campaign, research_product as product
from trade_system.v2 import research_followup as followup
from trade_system.v2.domain import identity
from trade_system.v2.gap_evidence import read_json
from tests.test_research_campaign import Fixture, declared_native
from tests.test_research_delivery import prediction, prices


class WindowFixture(Fixture):
    def query(self, request):
        if request['kind']=='calendar':
            start=date.fromisoformat(request['params']['start_date'][:4]+'-'+request['params']['start_date'][4:6]+'-'+request['params']['start_date'][6:])
            end=date.fromisoformat(request['params']['end_date'][:4]+'-'+request['params']['end_date'][4:6]+'-'+request['params']['end_date'][6:])
            days=[start+timedelta(days=i) for i in range((end-start).days+1)]
            return {'fields':campaign.FIELDS['trade_cal'],'items':[[request['params']['exchange'],d.strftime('%Y%m%d'),int(d.weekday()<5),''] for d in days]}
        return super().query(request)


def capture_config(start,end):
    return {'codes':['000001.SZ','600001.SH'],'start':start,'end':end,
            'window_days':90,'max_requests':300,'selection_scope':'synthetic_bounded_test'}


def test_cli_and_http_share_update_owner_before_any_work(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(product,'_update',lambda *a,**k:calls.append(1))
    with FileLock(tmp_path/'update.guard'):
        with pytest.raises(FileLockBusy):product.update(tmp_path,tmp_path)
    assert calls==[]
    product.update(tmp_path,tmp_path)
    assert calls==[1] and (tmp_path/'update.guard').exists()


def test_saved_judgement_survives_renderer_failure_without_running_renderer(tmp_path,monkeypatch):
    p=prediction()
    monkeypatch.setattr(product,'read_prediction',lambda _:p)
    def forbidden(*args,**kwargs):raise AssertionError('must not render, fetch or load model')
    monkeypatch.setattr(product,'publish_desk',forbidden)
    monkeypatch.setattr(product,'read_build',forbidden)
    values={'prediction_id':p['prediction_id'],'instrument':'000001','operator':'SYNTHETIC ONLY',
            'intent':'reject','hypothesis':'fixture reason','invalidation':'fixture condition'}
    note_id=product.save_note(tmp_path,values)
    note=read_json(tmp_path/'notes'/(note_id+'.json'))[0]
    assert note['intent']=='reject'
    data=product.journal_projection(tmp_path,{'reviews':[],'prediction':p})
    assert data['notes'][0]['note_id']==note_id and data['note_scope']['total']==1
    assert note['execution_ready'] is False


def test_pending_review_keeps_original_queue_and_absent_judgements():
    p=prediction();frame,days=prices(3)
    result=followup.evaluate(p,frame,days[:1],'2025-01-01T18:00:00+08:00')
    assert result['status']=='pending_exact_future_sessions'
    assert [r['instrument'] for r in result['rows']]==[r['instrument'] for r in p['rows']]
    assert all(r['target_pct'] is None for r in result['rows'])
    mature=followup.evaluate(p,frame,days,'2025-01-03T16:00:00+08:00')
    assert not mature['selection_eligible'] and 'comparison' not in mature


def test_incremental_history_matches_full_capture_and_keeps_raw_provenance(tmp_path):
    first=tmp_path/'first';delta=tmp_path/'delta';full=tmp_path/'full'
    campaign.capture(first,config=capture_config('2025-01-01','2025-01-10'),client=WindowFixture());declared_native(first)
    campaign.capture(delta,config=capture_config('2025-01-08','2025-01-13'),client=WindowFixture(),base=first);declared_native(delta)
    campaign.capture(full,config=capture_config('2025-01-01','2025-01-13'),client=WindowFixture());declared_native(full)
    actual=campaign.derive(delta);expected=campaign.derive(full)
    def facts(rows):
        return sorted([{k:v for k,v in r.items() if k!='receipt_files'} for r in rows],key=lambda r:(r['instrument'],r['datetime']))
    assert facts(actual['rows'])==facts(expected['rows'])
    assert actual['calendar']==expected['calendar']
    historical=next(r for r in actual['rows'] if r['datetime']=='2025-01-01')
    assert Path(historical['receipt_files']['native']).is_absolute()
    assert read_json(delta/'registration.json')[0]['base_manifest_id']==identity(campaign.sealed(first))
    (first/'receipt-00.json').write_text('{}')
    with pytest.raises(ValueError,match='changed'):campaign.derive(delta)


def test_incremental_rejects_foreign_universe_and_unbounded_gap(tmp_path):
    first=tmp_path/'first'
    campaign.capture(first,config=capture_config('2025-01-01','2025-01-10'),client=WindowFixture())
    for change in ('universe','gap'):
        config=capture_config('2025-01-08','2025-01-13')
        if change=='universe':config['codes']=['000002.SZ']
        else:config['end']='2025-02-20'
        with pytest.raises(ValueError,match='same universe'):
            campaign.capture(tmp_path/change,config=config,client=WindowFixture(),base=first)
        assert not (tmp_path/change).exists()


def test_derivation_can_reuse_already_verified_parse(tmp_path,monkeypatch):
    folder=tmp_path/'raw'
    campaign.capture(folder,config=capture_config('2025-01-01','2025-01-10'),client=WindowFixture());declared_native(folder)
    verified=campaign.replay(folder)
    def forbidden(*args):raise AssertionError('second parse')
    monkeypatch.setattr(campaign,'replay',forbidden)
    assert campaign.derive(folder,replayed=verified)['rows']


def test_projection_excludes_broad_and_unknown_members_without_fabricating_mainline():
    from trade_system.review_facts import workspace_snapshot
    from trade_system.daily_review import build_review_narrative
    context={'trade_date':'2025-01-01','concept_limit_up':{'groups':[
        {'concept_name':'融资融券','member_count':3800,'limit_up_count':50},
        {'concept_name':'missing_count','limit_up_count':20},
        {'concept_name':'focused','member_count':12,'limit_up_count':3}]}}
    original=deepcopy(context)
    assert build_review_narrative(context)['mainline']=='focused'
    snapshot=workspace_snapshot(context)
    assert [r['concept_name'] for r in snapshot['themes']]==['focused']
    assert snapshot['account_state']=='unknown' and context==original


def test_release_runtime_has_no_tools_dependency():
    from tools.v2.package_research import source_closure
    files=source_closure()
    assert not any(p.startswith('tools/') for p in files)


@pytest.mark.parametrize('calendar_end,force,expected_reuse',[
    ('2026-09-12',False,True),('2026-09-11',False,False),('2026-09-12',True,False)])
def test_session_reuse_requires_calendar_coverage_and_respects_refresh(tmp_path,monkeypatch,calendar_end,force,expected_reuse):
    from datetime import datetime, timezone
    scope='same_registered_research_universe_not_limit_pool_or_full_market'
    source=tmp_path/'receipts';calls=[]
    monkeypatch.setattr(product,'read_build',lambda _: (tmp_path,{'model_id':'synthetic'},{}))
    monkeypatch.setattr(product,'read_json',lambda _: ({'universe':['000001']},None))
    monkeypatch.setattr(product,'now_utc',lambda:datetime(2026,9,12,4,tzinfo=timezone.utc))
    monkeypatch.setattr(product,'read_prediction',lambda _:{'receipt_folder':str(source),'date':'2026-09-11',
        'model_id':'synthetic','predictions':1,'prediction_id':'synthetic-not-real'})
    saved={'codes':['000001.SZ'],'selection_scope':scope,'start':'2026-06-14','end':'2026-09-11'}
    def replay(folder,cache):
        result=({'config':saved,'origin':'native_and_relay'},[],{0:['2026-09-11',calendar_end]},None)
        cache[str(folder.resolve())]=result
        return result
    monkeypatch.setattr(campaign,'cached_replay',replay)
    monkeypatch.setattr(product,'latest_closed_session',lambda *args:'2026-09-11')
    monkeypatch.setattr(campaign,'derive',lambda *args,**kwargs:calls.append('validate_lineage'))
    def capture(*args,**kwargs):
        calls.append('refresh_required')
        raise RuntimeError('synthetic stop before network')
    monkeypatch.setattr(campaign,'capture',capture)
    if expected_reuse:
        result=product.update(tmp_path,tmp_path,force_refresh=force)
        assert result['provider_requests']==0 and result['fits']==0
        assert calls==['validate_lineage']
    else:
        with pytest.raises(RuntimeError,match='before network'):
            product.update(tmp_path,tmp_path,force_refresh=force)
        assert calls==['refresh_required']
