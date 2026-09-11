from copy import deepcopy

import pandas as pd
import pytest

from trade_system.v2 import price_study as study, research_product as product
from trade_system.v2.gap_evidence import write_json, read_json
from tests.test_research_delivery import prices


def inputs():
    days=['2026-06-15','2026-06-16']
    rows=[{'datetime':d,'instrument':'002414','adj_factor':None,'gaps':['daily_factor_missing'],
        'open':None,'high':None,'low':None,'close':None,'volume':None,'turnover':None,'net_mf_amount':123.} for d in days]
    raw={('002414.SZ',d):{'open':10,'high':12,'low':9,'close':11,'volume_shares':200,'turnover_cny':2100} for d in days}
    factors={d:{'adj_factor':2} for d in days}
    request={'code':'002414.SZ','api':'adj_factor','start':'2026-06-14','end':'2026-09-11'}
    return {'calendar':{'SZSE':days,'SSE':days},'rows':rows},raw,factors,request


def test_supplement_only_factor_and_derived_price_original_immutable():
    source,raw,factors,request=inputs();before=deepcopy(source)
    lineage={'received_at':'2026-09-11T15:00:00+00:00'}
    result,dates=study.supplement_rows(source,raw,factors,request,lineage)
    assert source==before and len(dates)==2
    row=result['rows'][0]
    assert row['close']==22 and row['volume']==100 and row['turnover']==2100
    assert row['net_mf_amount']==123 and row['adj_factor']==2
    lineage['extra']='later manifest field'
    assert 'extra' not in row['factor_supplement']


def test_price_conflict_not_cleared_by_factor_supplement():
    source,raw,factors,request=inputs()
    source['rows'][0]['gaps'].append('source_price_conflict')
    result,_=study.supplement_rows(source,raw,factors,request,{})
    assert result['rows'][0]['gaps']==['source_price_conflict']
    assert result['rows'][0]['close'] is None


@pytest.mark.parametrize('kind',['overwrite','missing_date','extra_date','wrong_code','wrong_api','zero','infinity'])
def test_factor_supplement_refuses_unapproved_or_incomplete_input(kind):
    source,raw,factors,request=inputs()
    if kind=='overwrite': source['rows'][0]['adj_factor']=2
    elif kind=='missing_date': factors.pop('2026-06-15')
    elif kind=='extra_date': factors['2026-06-17']={'adj_factor':2}
    elif kind=='wrong_code': request['code']='000001.SZ'
    elif kind=='wrong_api': request['api']='daily'
    elif kind=='zero': factors['2026-06-15']['adj_factor']=0
    else: factors['2026-06-15']['adj_factor']=float('inf')
    with pytest.raises(ValueError): study.supplement_rows(source,raw,factors,request,{})


def test_selection_preserves_sparse_dates_and_uses_same_eligible_denominator():
    model=[{'date':d,'samples':n,'mse':1,'top_k_label_mean_pct':v} for d,n,v in [('a',4,999),('b',6,2)]]
    old=[dict(r,top_k_label_mean_pct=3) for r in model]
    rules=[{'date':r['date'],'samples':r['samples'],'momentum_top5_target_pct':4,'equal_weight_target_pct':5} for r in model]
    result=study.summarize_selection(model,old,rules)
    assert result['all_samples']==10 and result['all_days']==2 and result['selection_days']==1
    assert result['days'][0]['model_top5'] is None
    assert result['days'][0]['model_mse']==1
    assert result['selection_only_means']=={'model_top5':2,'old_top5':3,'momentum_top5':4,'equal_weight':5}
    rules[0]['samples']=3
    with pytest.raises(ValueError): study.summarize_selection(model,old,rules)
    rules[0]['date']='other'
    with pytest.raises(ValueError): study.summarize_selection(model,old,rules)


def test_price_features_preserve_calendar_and_actual_target_without_alpha_dependency():
    frame,days=prices(100)
    frame.loc[(frame.instrument=='000001')&(frame.datetime==days[50]),'close']=None
    observed={'rows':frame.to_dict('records'),'calendar':{'SSE':days,'SZSE':days}}
    features=study.price_features(observed,days[60])
    assert len(features)==80
    row=features[(features.instrument=='000001')&(features.datetime==pd.Timestamp(days[80]))].iloc[0]
    assert not row.feature_eligible and row.price_eligible and pd.notna(row.label_next_ret)
    assert features.groupby('instrument').tail(2).label_next_ret.isna().all()


def test_failed_price_study_publication_restores_only_its_pointer(tmp_path,monkeypatch):
    write_json(tmp_path/'research-current.json',{'model':'original'})
    write_json(tmp_path/'price-study-current.json',{'folder':'previous'})
    def fail(_): raise ValueError('render failed')
    monkeypatch.setattr(product,'_publish_desk',fail)
    with pytest.raises(ValueError): product.publish_state(tmp_path,'price-study-current.json',{'folder':'new'})
    assert read_json(tmp_path/'price-study-current.json')[0]=={'folder':'previous'}
    assert read_json(tmp_path/'research-current.json')[0]=={'model':'original'}


def test_study_read_rejects_modified_evidence(tmp_path,monkeypatch):
    from trade_system.v2.domain import file_hash
    folder=tmp_path/'study';folder.mkdir()
    write_json(folder/'result.json',{'study_id':'fixed'})
    write_json(folder/'completed.json',{'artifact_hashes':{'result.json':file_hash(folder/'result.json')}})
    write_json(tmp_path/'price-study-current.json',{'folder':str(folder),'sha256':file_hash(folder/'completed.json')})
    monkeypatch.setattr(study.research,'read_result',lambda _: {})
    assert study.read(tmp_path)['study_id']=='fixed'
    (folder/'result.json').write_text('{"study_id":"tampered"}',encoding='utf-8')
    with pytest.raises(ValueError,match='artifact changed'):study.read(tmp_path)


def test_study_pointer_cannot_escape_output(tmp_path):
    write_json(tmp_path/'price-study-current.json',{'folder':str(tmp_path.parent),'sha256':'untrusted'})
    with pytest.raises(ValueError,match='pointer changed'):study.read(tmp_path)
