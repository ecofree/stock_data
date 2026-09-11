"""Untuned rule baselines evaluated on the exact registered QLib test identities."""
from pathlib import Path

import numpy as np
import pandas as pd

from . import rolling_research as research
from .domain import file_hash, identity
from .gap_evidence import read_json, write_json


CONTRACT={'momentum_rule':'descending adjusted 20-session return; ties by instrument',
          'equal_weight_rule':'all securities in each identical evaluable daily cross-section',
          'scope':'post_hoc_declared_fixed_rules_not_tuned_or_an_untouched_test',
          'mse':None,'reason':'ranking score is not a calibrated return prediction'}


def build(run, result):
    run=Path(run);frame=pd.read_parquet(run/'dataset/features.parquet')
    frame['datetime']=pd.to_datetime(frame.datetime).dt.strftime('%Y-%m-%d')
    grid=frame.set_index(['datetime','instrument']).sort_index()
    if not grid.index.is_unique: raise ValueError('duplicate benchmark identity')
    daily=[];folds=[]
    for fold in result['folds']:
        ids=read_json(run/'experiment/run'/f"fold-{fold['fold']:02d}"/'test_ids.json')[0]
        if identity(ids)!=fold['test_identity_sha256']:raise ValueError('test cohort changed')
        part=grid.loc[pd.MultiIndex.from_tuples([tuple(x) for x in ids],names=grid.index.names)].reset_index()
        if not np.isfinite(part[['ret_20d','label_next_ret']]).all().all():raise ValueError('common baseline cohort incomplete')
        for day,g in part.groupby('datetime',sort=True):
            g=g.sort_values(['ret_20d','instrument'],ascending=[False,True])
            ic=g.ret_20d.rank().corr(g.label_next_ret.rank()) if g.ret_20d.nunique()>1 and g.label_next_ret.nunique()>1 else None
            daily.append({'date':day,'samples':len(g),'rank_ic':float(ic) if ic is not None else None,
                'momentum_top5_target_pct':float(g.head(5).label_next_ret.mean()),
                'equal_weight_target_pct':float(g.label_next_ret.mean())})
        folds.append({'fold':fold['fold'],'test_identity_sha256':identity(ids),'samples':len(part)})
    if len({d['date'] for d in daily})!=len(daily):raise ValueError('repeated out-of-sample date')
    summary={'samples':sum(d['samples'] for d in daily),'days':len(daily),'mse':None,
        'mean_daily_rank_ic':float(np.mean([d['rank_ic'] for d in daily if d['rank_ic'] is not None])) if any(d['rank_ic'] is not None for d in daily) else None,
        'momentum_top5_target_pct':float(np.mean([d['momentum_top5_target_pct'] for d in daily])),
        'equal_weight_target_pct':float(np.mean([d['equal_weight_target_pct'] for d in daily]))}
    value={'contract':CONTRACT,'summary':summary,'folds':folds,'daily':daily,
        'feature_sha256':file_hash(run/'dataset/features.parquet'),'source_sha256':file_hash(__file__),
        'execution_ready':False,'portfolio_backtest':False}
    value['baseline_id']=identity(value);write_json(run/'rule-baselines.json',value)
    return value


def read(run):
    value=read_json(Path(run)/'rule-baselines.json')[0]
    if value['baseline_id']!=identity({k:v for k,v in value.items() if k!='baseline_id'}):raise ValueError('rule baseline changed')
    if value['feature_sha256']!=file_hash(Path(run)/'dataset/features.parquet'):raise ValueError('baseline input changed')
    research.read_result(Path(run)/'experiment')
    return value
