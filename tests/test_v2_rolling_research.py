from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from trade_system.v2.domain import canonical
from trade_system.v2.rolling_research import (FoldDataset, daily_metrics, file_hash, fold_frames, load_frame,
    paired_comparison, read_registration, read_result, register_experiment, run_experiment, summarize, validate_plan)


@pytest.fixture
def inputs(tmp_path):
    days = list(pd.bdate_range('2025-01-02',periods=50).strftime('%Y-%m-%d'))
    rows = [{'datetime':d,'instrument':f'{j+1:06d}','f':float(i+j),'g':float(j-i),'label_next_ret':float(j-2),
             'label_end_time':days[i+2]+' 15:00:00','label_available_time':days[i+2]+' 16:00:00'}
             for i,d in enumerate(days[:45]) for j in range(4)]
    path = tmp_path/'features.csv'; pd.DataFrame(rows).to_csv(path,index=False)
    metadata = {'feature_columns':['f','g'],'label_definition':'fixture next-session return pct',
                'availability_assumption':'fixture naive timestamps Asia/Shanghai',
                'artifact_hashes':{path.name:file_hash(path)}}
    path.with_suffix('.metadata.json').write_text(canonical(metadata),encoding='utf-8')
    plan = {'experiment_id':'fixture-v1','scope':'historical_research_only','exposure_status':'previously_inspected_not_untouched',
            'evaluation_asof':days[-1]+'T18:00:00+08:00','label_definition':metadata['label_definition'],
            'availability_assumption':metadata['availability_assumption'],'max_rows':180,'train_observations':10,
            'valid_observations':5,'test_observations':5,'excluded_tail_observations':5,'max_folds':5,
            'num_boost_round':2,'num_threads':1,'seed':17,'top_k':2,'round_trip_cost_bps':[0,10,50],
            'comparison_baseline':'price','variants':{'price':['f'],'funds':['f','g']}}
    return path,plan,tmp_path,days


def fake_fit(frames,features,plan,output):
    ds = FoldDataset(frames,features)
    assert 'test' not in ds.frames
    prediction_ds = FoldDataset(frames,features,fitting=False)
    assert 'label_next_ret' not in prediction_ds.frames['test']
    prediction = pd.Series(float(frames['train'].label_next_ret.mean()),index=prediction_ds.prepare('test').index)
    (output/'model.txt').write_text('fixture no real qlib model',encoding='utf-8')
    return prediction,{'medians':ds.medians.to_dict(),'backend':'fixture'}


def test_runner_same_identities_all_variants_separate_tail_and_readback(inputs):
    path,plan,root,_ = inputs
    folder = register_experiment(root/'registry',path,plan)
    result = run_experiment(folder,fit=fake_fit)
    assert len(result['folds'])==5 and result['excluded_tail_scored'] is False
    assert result['execution_ready'] is False and result['signal_impact']=='disabled'
    assert result['runtime']['model_backend']=='injected_test_backend_not_qlib'
    assert len({v['samples'] for v in result['aggregate'].values()})==1
    assert all(f['partition']['test_end']<result['excluded_tail'][0] for f in result['folds'])
    assert read_result(folder)==result
    assert (folder/'run/review.md').exists()
    with pytest.raises(FileExistsError): run_experiment(folder,fit=fake_fit)


def test_identity_sampling_is_not_selected_by_future_label(inputs):
    path,_,root,_ = inputs
    first = load_frame(path,['f','g'],90)
    data = pd.read_csv(path,dtype={'instrument':str}); data.loc[::2,'label_next_ret']=np.nan
    altered = root/'changed.csv'; data.to_csv(altered,index=False)
    second = load_frame(altered,['f','g'],90)
    pd.testing.assert_frame_equal(first[['datetime','instrument']],second[['datetime','instrument']])
    assert set(first.instrument)<=set(data.instrument)


def test_same_day_label_known_at_1600_does_not_enter_midnight_fit(inputs):
    path,_,_,days = inputs
    frame = load_frame(path,['f','g'],180)
    fold = {'train_end':days[9],'valid_start':days[10],'valid_end':days[14],'test_start':days[15],'test_end':days[19]}
    frames,coverage = fold_frames(frame,fold,days[49]+'T18:00:00+08:00',days[40])
    assert frames['train'].datetime.max()==days[7]
    assert frames['valid'].datetime.max()==days[12]
    assert pd.Timestamp(coverage['train_label_available_max'])<pd.Timestamp(days[10]+'T00:00:00+08:00')


def test_training_only_medians_and_test_labels_hidden(inputs):
    path,_,_,days = inputs
    frame = load_frame(path,['f','g'],180)
    fold = {'train_end':days[9],'valid_start':days[10],'valid_end':days[14],'test_start':days[15],'test_end':days[19]}
    frames,_ = fold_frames(frame,fold,days[-1]+'T18:00:00+08:00',days[40])
    frames['train']['g']=np.nan
    frames['test']['f']=10**9
    with pytest.raises(ValueError,match='all_missing'):
        FoldDataset(frames,['f','g'])
    ds = FoldDataset(frames,['f'])
    assert ds.medians['f']==frames['train'].f.median()
    with pytest.raises(ValueError,match='unavailable'): ds.prepare('test')
    predict = FoldDataset(frames,['f'],fitting=False)
    with pytest.raises(ValueError,match='labels unavailable'): predict.prepare('test',col_set='label')


@pytest.mark.parametrize('change',['labels','budget','cost','variant','exposure','seed','empty_features'])
def test_invalid_research_contract_rejected(inputs,change):
    _,plan,_,_ = inputs
    if change=='labels': plan['variants']['price']=['label_next_ret']
    if change=='budget': plan['num_threads']=8
    if change=='cost': plan['round_trip_cost_bps']=[50,0]
    if change=='variant': plan['comparison_baseline']='absent'
    if change=='exposure': plan['exposure_status']='untouched'
    if change=='seed': plan['seed']=True
    if change=='empty_features': plan['variants']['price']=[]
    with pytest.raises(ValueError): validate_plan(plan)


@pytest.mark.parametrize('change',['bytes','metadata','registration','source','runtime'])
def test_registry_changes_fail_closed(inputs,change,monkeypatch):
    path,plan,root,_ = inputs
    folder = register_experiment(root/'registry',path,plan)
    if change=='bytes': path.write_text(path.read_text()+'\n',encoding='utf-8')
    if change=='metadata': path.with_suffix('.metadata.json').write_text('{}',encoding='utf-8')
    if change=='registration':
        data = json.loads((folder/'registration.json').read_text()); data['plan']['num_boost_round']=10
        (folder/'registration.json').write_text(json.dumps(data),encoding='utf-8')
    if change=='source': monkeypatch.setattr('trade_system.v2.rolling_research.source_fingerprint',lambda:{})
    if change=='runtime': monkeypatch.setattr('trade_system.v2.rolling_research.runtime_versions',lambda:{})
    with pytest.raises(ValueError): read_registration(folder)
    assert not (folder/'run').exists()


def test_failed_prediction_keeps_failed_marker_not_completion(inputs):
    path,plan,root,_ = inputs
    folder = register_experiment(root/'registry',path,plan)
    def bad_fit(*args):
        p,details = fake_fit(*args); p.iloc[0]=np.inf; return p,details
    with pytest.raises(ValueError,match='finite'): run_experiment(folder,fit=bad_fit)
    assert (folder/'run/failed.json').exists() and not (folder/'run/completed.json').exists()
    with pytest.raises(FileExistsError): run_experiment(folder,fit=fake_fit)


def test_negative_results_and_cost_proxy_not_promoted_to_portfolio(inputs):
    path,_,_,_ = inputs
    test = load_frame(path,['f'],180)
    y = test.set_index(['datetime','instrument']).sort_index().label_next_ret
    daily = daily_metrics(-y,test,2)
    result = summarize(daily,[0,50])
    assert result['mean_daily_rank_ic']==-1 and result['portfolio_return'] is None
    assert result['cost_sensitivity_label_proxy_pct']['50']==result['top_k_mean_label_pct']-.5
    comparison = paired_comparison(daily,daily,17)
    assert comparison['mean_daily_mse_difference']==0 and comparison['moving_block_bootstrap_95pct']==[0,0]


@pytest.mark.parametrize('change',['duplicate','chronology','empty_date','tiny_budget'])
def test_feature_data_guardrails(inputs,change):
    path,_,_,_ = inputs
    data = pd.read_csv(path,dtype={'instrument':str})
    if change=='duplicate': data = pd.concat([data,data.iloc[:1]])
    if change=='chronology': data.loc[0,'label_available_time']='2020-01-01'
    if change=='empty_date': data.loc[0,'datetime']=None
    data.to_csv(path,index=False)
    with pytest.raises(ValueError): load_frame(path,['f'],1 if change=='tiny_budget' else 180)


def test_output_manifest_detects_changes(inputs):
    path,plan,root,_ = inputs
    folder = register_experiment(root/'registry',path,plan); run_experiment(folder,fit=fake_fit)
    (folder/'run/review.md').write_text('overwritten',encoding='utf-8')
    with pytest.raises(ValueError,match='checksum'): read_result(folder)


def test_registry_does_not_reuse_same_id_or_stale_export(inputs):
    path,plan,root,_ = inputs
    register_experiment(root/'registry',path,plan)
    with pytest.raises(FileExistsError): register_experiment(root/'registry',path,plan)
    new_plan = deepcopy(plan); new_plan['experiment_id']='other'
    path.write_text(path.read_text()+'\n',encoding='utf-8')
    with pytest.raises(ValueError,match='export manifest'): register_experiment(root/'registry',path,new_plan)


@pytest.mark.parametrize('change',['asof','fold_budget'])
def test_future_features_and_oversize_fold_count_fail(inputs,change):
    path,plan,root,days = inputs
    if change=='asof': plan['evaluation_asof']=days[20]+'T18:00:00+08:00'
    else: plan['max_folds']=1
    folder = register_experiment(root/'registry',path,plan)
    with pytest.raises(ValueError): run_experiment(folder,fit=fake_fit)
    assert not (folder/'run/completed.json').exists()


def test_csv_parquet_keep_same_identity_sample(inputs):
    import duckdb
    path,_,root,_ = inputs
    parquet = root/'features.parquet'
    with duckdb.connect(':memory:') as con:
        con.execute("CREATE TABLE source AS SELECT * FROM read_csv(?, types={'instrument':'VARCHAR'})",[str(path)])
        con.execute('COPY source TO ? (FORMAT PARQUET)',[str(parquet)])
    pd.testing.assert_frame_equal(load_frame(path,['f','g'],90),load_frame(parquet,['f','g'],90))


def test_prediction_missing_rows_cannot_gain_a_better_score(inputs):
    path,_,_,_ = inputs
    test = load_frame(path,['f'],180)
    y = test.set_index(['datetime','instrument']).sort_index().label_next_ret
    with pytest.raises(ValueError,match='exactly cover'):
        daily_metrics(y.iloc[1:],test,2)


def test_excluded_tail_labels_never_enter_fold_fitting(inputs):
    path,plan,root,days = inputs
    seen = []
    def inspect_fit(frames,features,plan,output):
        seen.extend(frames['train'].datetime.tolist()+frames['valid'].datetime.tolist()+frames['test'].datetime.tolist())
        return fake_fit(frames,features,plan,output)
    folder = register_experiment(root/'registry',path,plan)
    run_experiment(folder,fit=inspect_fit)
    assert max(seen)<days[40]
