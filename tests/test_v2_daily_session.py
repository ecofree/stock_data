from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

import duckdb
import pytest

from trade_system.v2.daily_session import (
    CALENDAR, POOL, CST, build_report, capture, verify, check_note, import_judgement,
    next_review, read_judgements, legacy_observations,
)
from trade_system.v2.daily_session_view import render, render_followup
from trade_system.v2.domain import canonical, utc


def moment(day='2026-09-10',time='16:00:00'):
    return utc(day+'T'+time+'+08:00')


def item(code='000002',height=2):
    return {'thscode':code+'.SZ','ticker':code,'name':'测试股份','last_price':10,
            'price_change_ratio_pct':10,'continue_day_cnt':height,'limit_up_reason':'来源归因',
            'is_st':False,'is_new':False}


def responses(day='2026-09-10',items=None,dates=None):
    items = [item()] if items is None else items
    days = dates or ['2026-09-09','2026-09-10']+(['2026-09-11'] if day=='2026-09-11' else [])
    stamp = int(moment(day).timestamp()*1000)
    ms = int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()*1000)
    return [{'path':CALENDAR,'params':{},'received_at':moment(day).isoformat(),
             'data':{'timestamp':stamp,'item':[{'date':d.replace('-','')} for d in days]}},
            {'path':POOL,'params':{'page':1,'date_ms':ms},'received_at':moment(day).isoformat(),
             'data':{'timestamp':stamp,'pagination':{'page':1,'pages':1,'total':len(items)},'item':items}}]


def report(day='2026-09-10',items=None,dates=None):
    return build_report(day,responses(day,items,dates),moment(day),origin='hithink_native')


class Fixture:
    def __init__(self,day='2026-09-10',dates=None):
        self.day=day
        self.dates=dates

    def _get(self,path,params):
        return deepcopy(next(r['data'] for r in responses(self.day,dates=self.dates) if r['path']==path))


def packet(r,**changes):
    return {'schema':1,'scope':'daily_judgement_no_execution','report_id':r['report_id'],
            'candidate_id':r['candidates'][0]['candidate_id'],'note_id':'intent-1',
            'created_at':moment(time='16:01:00').isoformat(),'operator':'test operator',
            'intent':'observe','hypothesis':'解释依据','invalidation':'失效条件',**changes}


def test_current_native_report_has_observation_not_execution():
    r=report()
    assert r['status']=='current_native_observation'
    assert r['execution_ready'] is False and r['actual_operator_return'] is None
    assert r['candidates'][0]['state']=='watch_only'
    assert 'row_event_time_not_proven' in r['gaps']


@pytest.mark.parametrize('mutation',[
    lambda r:r[1]['params'].update(date_ms=1),
    lambda r:r[1]['data']['pagination'].update(pages=2),
    lambda r:r[1]['data']['pagination'].update(total=2),
    lambda r:r[1]['data']['pagination'].update(page=2),
    lambda r:r[1]['data'].update(timestamp=int(moment(time='17:00:00').timestamp()*1000)),
    lambda r:r[0]['data']['item'].append(r[0]['data']['item'][0]),
])
def test_source_date_pagination_and_clock_fail_closed(mutation):
    r=responses();mutation(r)
    with pytest.raises(ValueError):
        build_report('2026-09-10',r,moment(),origin='hithink_native')


def test_stale_response_unknown_calendar_and_special_status_visible():
    r=responses(items=[{**item(),'is_st':None,'is_new':True}])
    r[0]['data']['item']=r[0]['data']['item'][:1]
    r[1]['data']['timestamp']=int(moment('2026-09-09').timestamp()*1000)
    d=build_report('2026-09-10',r,moment(),origin='hithink_native')
    assert d['status']=='source_check_required'
    assert 'source_response_not_current_day' in d['gaps']
    assert 'special_status_unknown' in d['candidates'][0]['risks']


def test_invalid_duplicates_not_silent_complete_pool():
    r=report(items=[item(),item(),{**item('000003'),'last_price':None}])
    assert r['pool_total']==3 and len(r['rejected_rows'])==2
    assert r['status']=='source_check_required'


def test_top_limit_retains_excluded_members_and_order():
    items=[item(str(i).zfill(6),i%4+1) for i in range(1,26)]
    a=report(items=items);b=report(items=list(reversed(items)))
    assert len(a['candidates'])==20 and len(a['not_selected_instruments'])==5
    assert [c['instrument'] for c in a['candidates']]==[c['instrument'] for c in b['candidates']]


def test_capture_is_sealed_and_cannot_overwrite_or_fake_native(tmp_path):
    folder=tmp_path/'day'
    r=capture('2026-09-10',folder,client=Fixture(),clock=moment)
    assert verify(folder)==r and r['origin']=='synthetic_fixture'
    with pytest.raises(FileExistsError):
        capture('2026-09-10',folder,client=Fixture(),clock=moment)
    with pytest.raises(ValueError):
        capture('2026-09-09',tmp_path/'old',client=Fixture(),clock=moment)
    (folder/'index.html').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError):
        verify(folder)


def test_capture_failure_keeps_receipt_no_completed_no_secret(tmp_path):
    class Broken:
        def _get(self,path,params):
            raise RuntimeError('private credential should not be saved')
    with pytest.raises(RuntimeError):
        capture('2026-09-10',tmp_path/'bad',client=Broken(),clock=moment)
    assert not (tmp_path/'bad/completed.json').exists()
    assert 'private credential' not in (tmp_path/'bad/failed.json').read_text()


@pytest.mark.parametrize('changes',[
    {'intent':'buy'}, {'candidate_id':'foreign'}, {'report_id':'foreign'},
    {'operator':''}, {'hypothesis':''}, {'scope':'execute'},
    {'created_at':'2026-09-10T00:00:00+08:00'}, {'command':'paper_buy'},
])
def test_judgement_strict_binding_and_nonexecution(changes):
    r=report()
    with pytest.raises(ValueError):
        check_note(packet(r,**changes),r)


def test_judgement_durable_retry_and_next_day_cohort(tmp_path):
    folder=tmp_path/'day'; archive=tmp_path/'judgements'
    r=capture('2026-09-10',folder,client=Fixture(),clock=moment)
    raw=canonical(packet(r)).encode()
    first=import_judgement(raw,folder,archive,clock=lambda:moment(time='16:02:00'))
    retry=import_judgement(raw,folder,archive,clock=lambda:moment('2026-09-11'))
    assert retry==first and len(list(archive.glob('*.json')))==1
    with pytest.raises(ValueError):
        import_judgement(canonical(packet(r,intent='reject')).encode(),folder,archive,clock=moment)
    with pytest.raises(ValueError):
        import_judgement(raw,folder,folder/'notes',clock=moment)
    notes=read_judgements(archive,r,asof=moment('2026-09-11'))
    later=report('2026-09-11',items=[])
    review=next_review(r,later,notes,created=moment('2026-09-11'))
    row=review['rows'][0]
    assert review['cohort_size']==1 and row['observation'] is None
    assert row['judgements'][0]['timing']=='recorded_before_next_open'
    assert row['in_following_observed_pool'] is False and row['actual_operator_return'] is None


def test_backdated_client_note_is_not_prospective_when_received_late(tmp_path):
    folder=tmp_path/'day';archive=tmp_path/'notes'
    r=capture('2026-09-10',folder,client=Fixture(),clock=moment)
    n=import_judgement(canonical(packet(r)).encode(),folder,archive,clock=lambda:moment('2026-09-11','10:00:00'))
    result=next_review(r,report('2026-09-11'),[n],created=moment('2026-09-11'))
    assert result['rows'][0]['judgements'][0]['timing']=='retrospective_not_prospective'


def test_no_skipped_session_future_or_wrong_date_price():
    r=report()
    following=report('2026-09-14',dates=['2026-09-10','2026-09-11','2026-09-14'])
    with pytest.raises(ValueError):
        next_review(r,following,[],created=moment('2026-09-14'))
    with pytest.raises(ValueError):
        next_review(r,report('2026-09-11'),[],created=moment())
    with pytest.raises(ValueError):
        next_review(r,report('2026-09-11'),[],created=moment('2026-09-11'),observations={'SZ.000002':{'date':'2026-09-12'}})


def test_html_escapes_provider_and_has_no_external_fetch():
    r=report(items=[{**item(),'name':'<script>alert(1)</script>'}])
    page=render(r)
    assert '<script>alert(1)</script>' not in page and '&lt;script&gt;' in page
    assert 'connect-src' in page and 'fetch(' not in page
    r['pool_total']=999
    with pytest.raises(ValueError):
        render(r)
    review=next_review(report(),report('2026-09-11'),[],created=moment('2026-09-11'))
    assert '未取得人工判断' in render_followup(review)
    review['rows'][0]['name']='changed'
    with pytest.raises(ValueError):
        render_followup(review)


def test_legacy_observations_read_only_exact_cohort_and_missing_not_zero(tmp_path):
    db=tmp_path/'legacy.duckdb'
    with duckdb.connect(str(db)) as c:
        c.execute('''CREATE TABLE tushare_daily(ts_code VARCHAR,date DATE,open DOUBLE,high DOUBLE,
          low DOUBLE,close DOUBLE,change_pct DOUBLE,fetched_at TIMESTAMP,adjustment VARCHAR,provider VARCHAR)''')
        c.execute("INSERT INTO tushare_daily VALUES ('000002.SZ','2026-09-11',10,11,9,10.5,5,'2026-09-11 15:30:00','none','xiaodefa')")
        c.execute("INSERT INTO tushare_daily VALUES ('000003.SZ','2026-09-11',10,11,9,10.5,5,'2026-09-12 15:30:00','none','xiaodefa')")
    bars=legacy_observations(db,'2026-09-11',['SZ.000002','SZ.000003'],asof=moment('2026-09-11'))
    assert set(bars)=={'SZ.000002'} and bars['SZ.000002']['source_status']=='legacy_table_not_native_authenticated'
    with pytest.raises(ValueError):
        legacy_observations(db,'2026-09-11',['SZ.000002'],asof=moment())


def test_actual_cli_pending_review_does_not_create_operator(tmp_path):
    folder=tmp_path/'day'
    capture('2026-09-10',folder,client=Fixture(),clock=moment)
    command=[sys.executable,'-m','trade_system.v2.daily_session_cli','review','--parent',str(folder),'--output',str(tmp_path/'pending')]
    p=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
    assert p.returncode==0,p.stderr
    saved=json.loads((tmp_path/'pending/review.json').read_text(encoding='utf-8'))
    assert saved['status']=='awaiting_next_native_session' and saved['judgements_received']==0


def test_actual_cli_judge_retry_followup_synthetic_only(tmp_path):
    parent=tmp_path/'day8';following=tmp_path/'day9';archive=tmp_path/'notes'
    r=capture('2026-09-08',parent,client=Fixture('2026-09-08',['2026-09-08']),clock=lambda:moment('2026-09-08'))
    capture('2026-09-09',following,client=Fixture('2026-09-09',['2026-09-08','2026-09-09']),clock=lambda:moment('2026-09-09'))
    note=packet(r,created_at=moment('2026-09-08','16:01:00').isoformat(),operator='synthetic fixture operator')
    path=tmp_path/'note.json';path.write_text(canonical(note),encoding='utf-8')
    base=[sys.executable,'-m','trade_system.v2.daily_session_cli']
    judge=['judge','--report',str(parent),'--note',str(path),'--archive',str(archive)]
    for args in [judge,judge,['review','--parent',str(parent),'--following',str(following),
                            '--archive',str(archive),'--output',str(tmp_path/'review')]]:
        result=subprocess.run(base+args,cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stdout+result.stderr
    saved=json.loads((tmp_path/'review/review.json').read_text(encoding='utf-8'))
    assert saved['cohort_size']==1 and len(list(archive.glob('*.json')))==1
    assert saved['rows'][0]['judgements'][0]['timing']=='retrospective_not_prospective'
    assert 'synthetic_fixture_not_real_market' in saved['source_gaps']


def test_before_close_response_not_final_even_received_after_close():
    source=responses()
    source[1]['data']['timestamp']=int(moment(time='14:30:00').timestamp()*1000)
    r=build_report('2026-09-10',source,moment(),origin='hithink_native')
    assert 'pool_response_before_close' in r['gaps']
    with pytest.raises(ValueError):
        next_review(r,report('2026-09-11'),[],created=moment('2026-09-11'))


def test_cli_view_does_not_mutate_source(tmp_path):
    folder=tmp_path/'day'
    r=capture('2026-09-10',folder,client=Fixture(),clock=moment)
    command=[sys.executable,'-m','trade_system.v2.daily_session_cli','view','--report',str(folder)]
    for target,expected in [(tmp_path/'view',0),(folder/'view',1)]:
        p=subprocess.run(command+['--output',str(target)],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
        assert p.returncode==expected,p.stdout+p.stderr
    assert verify(folder)==r
