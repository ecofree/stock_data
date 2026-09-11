from copy import deepcopy

import pytest

from tests.test_v2_daily_session import report,moment
from trade_system.v2 import prospective_samples as samples


def setup(monkeypatch,day='2026-09-11'):
    r=report(day)
    e={'parent_report_id':r['report_id'],'trade_date':day,'enrichment_id':'fixture',
        'origin':'synthetic_fixture','observations':{},'topics':{}}
    monkeypatch.setattr(samples.prospective_registry,'inspect',lambda *a,**kw:{'calendar_window':'open','registration_id':'fixture-reg'})
    real=samples.read_json
    monkeypatch.setattr(samples,'read_json',lambda path: ({'start_date':'2026-09-11','end_date':'2026-12-31',
        'minimum_paired_sessions':20,
        'variants':{'price_baseline':['ret_1d'],'plus_flow':['ret_1d','flow']}},None) if path.name=='protocol.json' else real(path))
    monkeypatch.setattr(samples.daily_session,'verify',lambda p:r)
    monkeypatch.setattr(samples.native_enrichment,'verify',lambda p:e)
    return r,e


def test_intake_keeps_missing_cases_not_fake_qualified_samples(tmp_path,monkeypatch):
    setup(monkeypatch)
    result=samples.freeze(tmp_path/'reg',tmp_path/'report',tmp_path/'enrich',tmp_path/'out',clock=lambda:moment('2026-09-11'))
    assert samples.read_sample(tmp_path/'out')==result
    assert result['origin']=='synthetic_fixture' and result['qualified_paired_sessions']==0
    assert result['rows'][0]['feature_groups']['plus_flow']['flow'] is None
    assert result['rows'][0]['price_observation'] is None and not result['rows'][0]['eligible_for_fit']


def test_no_retrospective_intake_or_pre_window_override(tmp_path,monkeypatch):
    setup(monkeypatch)
    with pytest.raises(ValueError,match='current session'):
        samples.freeze(tmp_path/'reg',tmp_path/'report',tmp_path/'enrich',tmp_path/'out',clock=lambda:moment('2026-09-14'))
    monkeypatch.setattr(samples.prospective_registry,'inspect',lambda *a,**kw:{'calendar_window':'not_started'})
    with pytest.raises(ValueError,match='not open'):
        samples.freeze(tmp_path/'reg',tmp_path/'report',tmp_path/'enrich',tmp_path/'out',clock=lambda:moment('2026-09-11'))
    assert not (tmp_path/'out').exists()


def test_raw_T2_receipts_cannot_create_adjusted_training_label(tmp_path,monkeypatch):
    r,e=setup(monkeypatch)
    samples.freeze(tmp_path/'reg',tmp_path/'report',tmp_path/'enrich',tmp_path/'out',clock=lambda:moment('2026-09-11'))
    following=deepcopy(r);following['calendar']+=['2026-09-14','2026-09-15']
    following['generated_at']=moment('2026-09-15').isoformat()
    monkeypatch.setattr(samples.daily_session,'verify',lambda p:following)
    def enrichment(path):
        date='2026-09-14' if str(path)=='one' else '2026-09-15'
        return {**e,'trade_date':date,'observations':{'SZ.000002':{'received_at':moment(date).isoformat(),
                'response_sha256':'fixture','adjustment':'none','open':'10','close':'11'}}}
    monkeypatch.setattr(samples.native_enrichment,'verify',enrichment)
    result=samples.maturity(tmp_path/'out','following','one','two',clock=lambda:moment('2026-09-15'))
    assert result['qualified_paired_sessions']==0 and result['rows'][0]['label_next_ret'] is None
    assert result['rows'][0]['label_status']=='adjusted_label_not_proven_from_raw_bars'
    following['calendar']=['2026-09-11','2026-09-15','2026-09-16']
    with pytest.raises(ValueError,match='exact T1/T2'):
        samples.maturity(tmp_path/'out','following','one','two',clock=lambda:moment('2026-09-16'))


def test_inventory_does_not_count_days_or_fixtures_as_qualified(tmp_path,monkeypatch):
    setup(monkeypatch)
    samples.freeze(tmp_path/'reg',tmp_path/'report',tmp_path/'enrich',tmp_path/'out',clock=lambda:moment('2026-09-11'))
    result=samples.queue_status(tmp_path/'reg',[tmp_path/'out'],clock=lambda:moment('2026-09-11'))
    assert result['observed_sessions']==1 and result['qualified_paired_sessions']==0
    assert result['missing_feature_observations']['plus_flow']['flow']==1 and not result['research_ready']
    with pytest.raises(ValueError,match='unique'):
        samples.queue_status(tmp_path/'reg',[tmp_path/'out',tmp_path/'out'],clock=lambda:moment('2026-09-11'))
    sample=samples.read_sample(tmp_path/'out');sample['rows'][0]['eligible_for_fit']=True
    monkeypatch.setattr(samples,'read_sample',lambda p:sample)
    with pytest.raises(ValueError,match='unqualified'):
        samples.queue_status(tmp_path/'reg',[tmp_path/'out'],clock=lambda:moment('2026-09-11'))
