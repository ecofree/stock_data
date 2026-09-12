"""Independent arithmetic on sealed OOS predictions, not a new unseen experiment."""
from pathlib import Path
import json
import numpy as np
import pandas as pd

from .domain import file_hash, identity, now_utc
from .gap_evidence import write_json, read_json

PROTOCOL={'schema':1,'scope':'independent_recalculation_of_previously_exposed_OOS_not_new_holdout',
    'top_k':5,'minimum_cross_section':6,'minimum_eligible_days':20,
    'block_sessions':5,'bootstrap_samples':2000,'seed':20260912,
    'comparisons':['momentum','equal_weight','previous_model'],
    'round_trip_cost_bps':[0,10,30,50],
    'target':'adjusted_next_open_to_second_session_close_price_proxy_not_portfolio_return',
    'promote_model':False,'fit_budget':0}


def evaluate(frame):
    required=['prediction','old_prediction','label_next_ret','ret_20d']
    if frame.duplicated(['datetime','instrument']).any():raise ValueError('duplicate paired identity')
    if not np.isfinite(frame[required]).all().all():raise ValueError('nonfinite paired value')
    days=[]
    for day,g in frame.groupby('datetime',sort=True):
        row={'date':str(day)[:10],'samples':len(g),'eligible':len(g)>=6}
        if row['eligible']:
            def top(column):return float(g.sort_values([column,'instrument'],ascending=[False,True],kind='stable').head(5).label_next_ret.mean())
            row.update(model=top('prediction'),previous_model=top('old_prediction'),momentum=top('ret_20d'),equal_weight=float(g.label_next_ret.mean()))
        days.append(row)
    good=[r for r in days if r['eligible']];comparisons={}
    for name in PROTOCOL['comparisons']:
        delta=np.array([r['model']-r[name] for r in good])
        if len(delta)<20:
            comparisons[name]={'status':'insufficient_eligible_days','days':len(delta)}
            continue
        rng=np.random.default_rng(PROTOCOL['seed']);means=[]
        # Blocks never bridge dates removed for insufficient cross-section.
        blocks=[np.array([r['model']-r[name] for r in days[i:i+5]]) for i in range(len(days)-4)
                if all(r['eligible'] for r in days[i:i+5])]
        if not blocks:
            comparisons[name]={'status':'insufficient_contiguous_blocks','days':len(delta)}
            continue
        for _ in range(PROTOCOL['bootstrap_samples']):
            sample=np.concatenate([blocks[i] for i in rng.integers(len(blocks),size=int(np.ceil(len(delta)/5)))])[:len(delta)]
            means.append(float(sample.mean()))
        low,high=map(float,np.quantile(means,[.025,.975]))
        comparisons[name]={'status':'descriptive_only','days':len(delta),'mean_excess_pct':float(delta.mean()),
            'ci95_pct':[low,high],'positive_days':int((delta>0).sum()),
            'positive_interval':low>0,'negative_interval':high<0}
    averages={name:float(np.mean([r[name] for r in good])) if good else None
              for name in ['model',*PROTOCOL['comparisons']]}
    return {'days':days,'eligible_days':len(good),'all_days':len(days),'samples':len(frame),
        'means_pct':averages,'comparisons':comparisons,
        'model_cost_proxy_pct':{str(c):averages['model']-c/100 if good else None for c in PROTOCOL['round_trip_cost_bps']},
        'superiority_proven':False,'promotion_allowed':False,
        'reason':'previously_exposed_selected_sample_and_price_proxy_cannot_establish_general_investment_utility',
        'execution_ready':False}


def verify(workspace,output):
    from .price_study import read
    workspace=Path(workspace).resolve();output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    protocol=dict(PROTOCOL,registered_at=now_utc().isoformat(),implementation_sha256=file_hash(__file__))
    write_json(output/'protocol.json',protocol)
    original=read(workspace)
    if original is None:raise ValueError('sealed independent price study required')
    pointer=read_json(workspace/'price-study-current.json')[0];folder=Path(pointer['folder'])
    features=pd.read_parquet(folder/'dataset/features.parquet')
    features['datetime']=pd.to_datetime(features.datetime).dt.strftime('%Y-%m-%d')
    parts=[];bound={str(folder/'dataset/features.parquet'):file_hash(folder/'dataset/features.parquet'),
        str(folder/'completed.json'):file_hash(folder/'completed.json')}
    for fold in sorted((folder/'experiment/run').glob('fold-*')):
        ids=read_json(fold/'test_ids.json')[0]
        bound[str(fold/'test_ids.json')]=file_hash(fold/'test_ids.json')
        new_path=fold/'price_baseline/predictions.csv';old_path=folder/'previous-model'/(fold.name+'.csv')
        def load(path):
            f=pd.read_csv(path,dtype={'instrument':str,'datetime':str})
            actual=list(map(list,f[['datetime','instrument']].itertuples(index=False,name=None)))
            if sorted(actual)!=sorted(ids):raise ValueError('prediction test identity mismatch')
            bound[str(path)]=file_hash(path)
            return f
        new=load(new_path);old=load(old_path).rename(columns={'prediction':'old_prediction'})
        paired=new.merge(old,on=['datetime','instrument'],validate='one_to_one')
        paired=paired.merge(features[['datetime','instrument','label_next_ret','ret_20d']],on=['datetime','instrument'],validate='one_to_one')
        if len(paired)!=len(ids):raise ValueError('missing label/feature for frozen test id')
        parts.append(paired)
    if not parts:raise ValueError('no sealed folds')
    result=evaluate(pd.concat(parts,ignore_index=True))
    expected=original['selection']['selection_only_means']
    mapping={'model':'model_top5','previous_model':'old_top5','momentum':'momentum_top5','equal_weight':'equal_weight'}
    differences={k:abs(result['means_pct'][k]-expected[v]) if result['means_pct'][k] is not None and expected[v] is not None else None for k,v in mapping.items()}
    if any(d is not None and d>1e-9 for d in differences.values()):raise ValueError('independent arithmetic differs from sealed summary')
    result.update(protocol_id=identity(protocol),study_id=original['study_id'],input_hashes=bound,
        arithmetic_differences=differences,source_study_unchanged=read(workspace)==original,new_fits=0,new_provider_requests=0)
    write_json(output/'result.json',result)
    return {k:result[k] for k in ['eligible_days','all_days','samples','means_pct','comparisons','superiority_proven','new_fits']}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(verify(args.workspace,args.output)))
