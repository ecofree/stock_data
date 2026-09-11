from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trade_system.v2 import research_dataset as ds, research_product as product
from trade_system.v2 import research_journal as journal, research_followup as followup
from trade_system.v2 import research_baselines as baselines
from trade_system.v2.domain import identity, file_hash
from trade_system.v2.gap_evidence import read_json, write_json
from tests.test_research_delivery import prices,prediction


def sealed(value):
    value=deepcopy(value);value.pop('prediction_id',None)
    return dict(value,prediction_id=identity(value))


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    write_json(path,value)


def test_actual_dependency_windows_do_not_change_old_cohort():
    frame,days=prices(90)
    frame.loc[(frame.instrument=='000001')&(frame.datetime==days[50]),'close']=np.nan
    f=ds.features(frame,days);latest=f[f.datetime==pd.Timestamp(days[-1])].set_index('instrument')
    assert latest.loc['000001','price_eligible'] and not latest.loc['000001','alpha158_window_eligible']
    assert not f[f.instrument=='000001'].feature_eligible.any()
    frame.loc[(frame.instrument=='000001')&(frame.datetime==days[-3]),'net_mf_amount']=np.nan
    f=ds.features(frame,days);r=f[(f.instrument=='000001')&(f.datetime==pd.Timestamp(days[-1]))].iloc[0]
    assert r.price_eligible and not r.money_eligible
    frame.loc[(frame.instrument=='000001')&(frame.datetime==days[-3]),'volume']=np.nan
    f=ds.features(frame,days);assert not f[(f.instrument=='000001')&(f.datetime==pd.Timestamp(days[-1]))].iloc[0].price_eligible


@pytest.mark.parametrize('moment,expected',[
    ('2026-09-11T15:59:59+08:00','2026-09-10'),('2026-09-11T16:00:00+08:00','2026-09-11'),
    ('2026-09-12T12:00:00+08:00','2026-09-11'),('2026-09-14T10:00:00+08:00','2026-09-11')])
def test_calendar_not_wall_clock_day_selects_closed_session(moment,expected):
    assert product.latest_closed_session(['2026-09-10','2026-09-11','2026-09-14'],moment)==expected


@pytest.mark.parametrize('entry,frozen,expected',[
    ('2026-09-14T09:30:00+08:00','2026-09-14T09:29:59+08:00','before_entry'),
    ('2026-09-14T09:30:00+08:00','2026-09-14T09:30:00+08:00','after_entry'),
    (None,'2026-09-11T17:00:00+08:00','pending_future_calendar')])
def test_forecast_timing_uses_final_freeze_not_capture_start(entry,frozen,expected):
    assert product.forecast_timing(entry,frozen)['forecast_status']==expected


def test_failed_publication_restores_pointer(tmp_path,monkeypatch):
    old={'path':'last_success'};write_json(tmp_path/'prediction-current.json',old)
    def fail(_):raise ValueError('simulated rendering failure')
    monkeypatch.setattr(product,'_publish_desk',fail)
    with pytest.raises(ValueError,match='simulated'):product.publish_state(tmp_path,'prediction-current.json',{'path':'new'})
    assert read_json(tmp_path/'prediction-current.json')[0]==old
    with pytest.raises(ValueError):product.publish_state(tmp_path,'research-current.json',{'build':'new'})
    assert not (tmp_path/'research-current.json').exists()


def test_weekend_forecast_is_valid_before_exact_next_entry():
    frame,_=prices(3);days=['2026-09-11','2026-09-14','2026-09-15'];frame['datetime']=days*2
    p=prediction('2026-09-11');p['captured_at']='2026-09-12T12:00:00+08:00';p['model_id']='same-model';p=sealed(p)
    assert followup.evaluate(p,frame,days,'2026-09-15T16:00:00+08:00')['paired']==1
    p['captured_at']='2026-09-14T09:30:00+08:00';p=sealed(p)
    assert followup.evaluate(p,frame,days,'2026-09-15T16:00:00+08:00')['status']=='not_prospective'


def test_same_day_model_repeated_refresh_is_not_repeated_performance(tmp_path):
    frame,days=prices(3);p=prediction();p['model_id']='same-model';p=sealed(p)
    q=dict(p,captured_at='2025-01-01T18:00:00+08:00');q=sealed(q)
    for name,value in [('a',p),('b',q)]:save(tmp_path/'observations'/name/'predictions.json',value)
    r=followup.collect(tmp_path,frame,days,'2025-01-03T16:00:00+08:00')
    assert len(r)==1 and r[0]['prediction_id']==p['prediction_id'] and r[0]['count_in_summary']


def test_judgement_revision_never_replaces_original_pre_entry_record(tmp_path,monkeypatch):
    p=sealed(dict(prediction(),scheduled_entry_at='2025-01-02T09:30:00+08:00',model_id='frozen'))
    monkeypatch.setattr(product,'read_prediction',lambda _:p);monkeypatch.setattr(product,'publish_desk',lambda _:None)
    monkeypatch.setattr(product,'now_utc',lambda:pd.Timestamp('2025-01-01T18:00:00+08:00'))
    values={'prediction_id':p['prediction_id'],'instrument':'000001','operator':'SYNTHETIC TEST',
        'intent':'observe','hypothesis':'original thesis','invalidation':'explicit risk'}
    first=product.save_note(tmp_path,values);before=file_hash(tmp_path/'notes'/(first+'.json'))
    monkeypatch.setattr(product,'now_utc',lambda:pd.Timestamp('2025-01-03T18:00:00+08:00'))
    revised=dict(values,hypothesis='after outcome note',supersedes=first)
    second=product.save_note(tmp_path,revised)
    assert file_hash(tmp_path/'notes'/(first+'.json'))==before
    assert read_json(tmp_path/'notes'/(second+'.json'))[0]['timing']=='after_entry'
    with pytest.raises(ValueError,match='already revised'):product.save_note(tmp_path,revised)
    notes=journal.annotate([read_json(path)[0] for path in (tmp_path/'notes').glob('*.json')])
    frame,days=prices(3);r=followup.evaluate(p,frame,days,'2025-01-03T19:00:00+08:00')
    paired=journal.attach([r],notes)[0]['rows'][0]
    assert paired['prospective_judgement']==first and len(paired['judgements'])==2
    assert paired['invalidation_review']=='manual_review_required'


def test_revised_historical_prediction_is_resolved_by_id(tmp_path):
    p=prediction();save(tmp_path/'observations/a/predictions.json',p)
    assert journal.find_prediction(tmp_path,p['prediction_id'])==p
    with pytest.raises(ValueError):journal.find_prediction(tmp_path,'../../other')


def test_rule_baseline_consumes_exact_test_cohort_not_other_dates(tmp_path):
    f,days=prices(3);f['label_next_ret']=np.arange(len(f));f['ret_20d']=np.arange(len(f))*.1
    (tmp_path/'dataset').mkdir();f.to_parquet(tmp_path/'dataset/features.parquet',index=False)
    ids=[[days[0],'000001'],[days[0],'600001']]
    save(tmp_path/'experiment/run/fold-00/test_ids.json',ids)
    result={'folds':[{'fold':0,'test_identity_sha256':identity(ids)}]}
    b=baselines.build(tmp_path,result)
    assert b['summary']['samples']==2 and b['summary']['days']==1 and b['summary']['mse'] is None
    assert b['summary']['equal_weight_target_pct']==pytest.approx(1.5)
    assert all(r['date']==days[0] for r in b['daily'])


def test_baseline_rejects_changed_test_identity(tmp_path):
    f,_=prices(3);(tmp_path/'dataset').mkdir();f.to_parquet(tmp_path/'dataset/features.parquet',index=False)
    save(tmp_path/'experiment/run/fold-00/test_ids.json',[])
    with pytest.raises(ValueError,match='changed'):
        baselines.build(tmp_path,{'folds':[{'fold':0,'test_identity_sha256':'wrong'}]})
