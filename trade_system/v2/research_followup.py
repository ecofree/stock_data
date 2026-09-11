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
    if not model_frozen<=frozen<=received or frozen.tz_convert('Asia/Shanghai').date()!=signal.date():
        raise ValueError('model and prediction receipt order invalid')
    result={'prediction_id':prediction['prediction_id'],'prediction_date':prediction['date'],
        'received_at':received_at,'rows':[],'paired':0,'status':'pending_exact_future_sessions',
        'execution_ready':False,'target_scope':'adjusted_price_target_not_executable_return'}
    if prediction['date'] not in days:
        result['status']='outside_current_receipt_window'; return result
    pos=days.index(prediction['date'])
    if pos+2>=len(days): return result
    t1,t2=days[pos+1:pos+3]
    if frozen>=pd.Timestamp(t1+'T09:30:00',tz='Asia/Shanghai'):
        raise ValueError('forecast received after target entry')
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
            'error_pct':score-target if paired else None,'status':state,'entry_date':t1,'exit_date':t2})
    result['status']='mature' if result['paired']==prediction['predictions'] else 'partial_missing_prices'
    return result


def collect(output, frame, calendar, received_at):
    import heapq
    files=heapq.nlargest(30,(Path(output)/'observations').glob('*/predictions.json'),key=lambda p:p.stat().st_mtime_ns)
    # The UI is a bounded recent window, not a growing embedded archive.
    predictions=[read_json(path)[0] for path in files]
    predictions=sorted(predictions,key=lambda p:p['captured_at'])[-30:]
    return [evaluate(p,frame,calendar,received_at) for p in predictions]
