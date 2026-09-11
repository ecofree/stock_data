"""Evaluate immutable pre-existing forecasts only against later received exact sessions."""
from pathlib import Path

import numpy as np
import pandas as pd

from .domain import identity
from .gap_evidence import read_json


def evaluate(prediction, frame, calendar, received_at):
    if prediction['prediction_id'] != identity({k:v for k,v in prediction.items() if k!='prediction_id'}):
        raise ValueError('prior prediction changed')
    days=list(calendar)
    if days!=sorted(set(days)): raise ValueError('ordered unique sessions required')
    if frame.duplicated(['instrument','datetime']).any(): raise ValueError('duplicate followup price')
    frozen=pd.Timestamp(prediction['captured_at'])
    model_frozen=pd.Timestamp(prediction['model_frozen_at'])
    received=pd.Timestamp(received_at)
    signal=pd.Timestamp(prediction['date'],tz='Asia/Shanghai')
    if any(t.tzinfo is None for t in (frozen,model_frozen,received)):
        raise ValueError('timezone-aware receipts required')
    if not model_frozen<=frozen<=received or frozen<signal:
        raise ValueError('model and prediction receipt order invalid')
    result={'prediction_id':prediction['prediction_id'],'prediction_date':prediction['date'],
        'received_at':received_at,'rows':[],'paired':0,'status':'pending_exact_future_sessions',
        'execution_ready':False,'target_scope':'adjusted_price_target_not_executable_return'}
    if prediction.get('prospective_eligible') is False:
        result['status']='not_prospective';return result
    if prediction['date'] not in days:
        result['status']='outside_current_receipt_window'; return result
    pos=days.index(prediction['date'])
    if pos+2>=len(days): return result
    t1,t2=days[pos+1:pos+3]
    if frozen>=pd.Timestamp(t1+'T09:30:00',tz='Asia/Shanghai'):
        result['status']='not_prospective';return result
    if received<pd.Timestamp(t2+'T16:00:00',tz='Asia/Shanghai'): return result
    grid=frame.copy(); grid['datetime']=pd.to_datetime(grid.datetime).dt.strftime('%Y-%m-%d')
    grid=grid.set_index(['instrument','datetime'])
    for row in prediction['rows']:
        code=row['instrument']; target=None; state='missing_exact_future_price'
        try:
            opening=float(grid.loc[(code,t1),'open']); closing=float(grid.loc[(code,t2),'close'])
            if np.isfinite([opening,closing]).all() and min(opening,closing)>0:
                target=(closing/opening-1)*100; state='mature_price_target'
        except KeyError: pass
        score=row['prediction']
        paired=target is not None and score is not None
        result['paired']+=int(paired)
        result['rows'].append({'instrument':code,'prediction':score,'target_pct':target,
            'baseline_momentum_20d':row.get('baseline_momentum_20d'),
            'error_pct':score-target if paired else None,'status':state,'entry_date':t1,'exit_date':t2})
    result['status']='mature' if all(r['target_pct'] is not None for r in result['rows']) else 'partial_missing_prices'
    paired=[r for r in result['rows'] if r['target_pct'] is not None and r['prediction'] is not None and r['baseline_momentum_20d'] is not None]
    result['common_rule_model_samples']=len(paired)
    if paired:
        model_top=sorted(paired,key=lambda r:(-r['prediction'],r['instrument']))[:5]
        rule_top=sorted(paired,key=lambda r:(-r['baseline_momentum_20d'],r['instrument']))[:5]
        result['comparison']={'model_top5_target_pct':float(np.mean([r['target_pct'] for r in model_top])),
            'momentum_top5_target_pct':float(np.mean([r['target_pct'] for r in rule_top])),
            'equal_weight_target_pct':float(np.mean([r['target_pct'] for r in paired])),
            'scope':'identical_mature_cross_section_not_portfolio_return'}
    return result


def collect(output, frame, calendar, received_at):
    # Stream metadata from immutable batches; keep first freeze per day/model,
    # rather than allowing repeated refreshes to multiply the score denominator.
    groups={};noted={}
    note_ids=set()
    import heapq
    for path in heapq.nlargest(100,(Path(output)/'notes').glob('*.json'),key=lambda p:p.stat().st_mtime_ns):
        note_ids.add(read_json(path)[0]['prediction_id'])
    for path in (Path(output)/'observations').glob('*/predictions.json'):
        p=read_json(path)[0]
        if p['prediction_id']!=identity({k:v for k,v in p.items() if k!='prediction_id'}):raise ValueError('archived forecast changed')
        if p['prediction_id'] in note_ids:noted[p['prediction_id']]=p
        if not p['predictions'] or p.get('prospective_eligible') is False:continue
        key=(p['date'],p.get('model_id','legacy'))
        if key not in groups or p['captured_at']<groups[key]['captured_at']:groups[key]=p
        if len(groups)>30:del groups[min(groups)]
    selected={p['prediction_id']:p for p in groups.values()}
    counted=set(selected);selected.update(noted)
    results=[]
    for p in sorted(selected.values(),key=lambda p:p['captured_at']):
        r=evaluate(p,frame,calendar,received_at)
        r['count_in_summary']=p['prediction_id'] in counted and r['status']!='not_prospective'
        results.append(r)
    return results
