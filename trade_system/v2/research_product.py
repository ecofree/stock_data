"""Local research delivery: existing dataset, QLib runner, observations and journal."""
import argparse
from datetime import timedelta
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from . import research_dataset as dataset, rolling_research as research
from .domain import canonical, file_hash, identity, now_utc
from .gap_evidence import read_json, write_json
from .publisher import publish, read_current


def write_pointer(root, name, value):
    temp=Path(root)/('.'+name+'-'+uuid.uuid4().hex)
    write_json(temp,value); temp.replace(Path(root)/name)


def build(config_path, root, output):
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    config=read_json(config_path)[0]
    run=output/'builds'/uuid.uuid4().hex; run.mkdir(parents=True)
    write_json(run/'configuration.json',config)
    meta=dataset.build(config,root,run/'dataset')
    plan=dataset.experiment_plan(config,meta,'delivery-'+run.name[:12])
    research.register_experiment(run,run/'dataset/features.parquet',dict(plan,experiment_id='experiment'))
    result=research.run_experiment(run/'experiment')
    research.read_result(run/'experiment')
    last=run/'experiment/run'/f"fold-{len(result['folds'])-1:02d}"/'price_baseline'
    model={'model_path':str(last/'model.txt'),'model_sha256':file_hash(last/'model.txt'),
        'preprocessing_path':str(last/'preprocessing.json'),'preprocessing_sha256':file_hash(last/'preprocessing.json'),
        'selection':'predeclared_last_fold_price_baseline_not_best_test_variant',
        'train_end':result['folds'][-1]['partition']['train_end'],
        'validation_end':result['folds'][-1]['partition']['valid_end'],
        'frozen_at':now_utc().isoformat(),'execution_ready':False}
    model['model_id']=identity(model)
    write_json(run/'frozen-model.json',model)
    pointer={'build':str(run),'dataset_id':meta['dataset_id'],'model_id':model['model_id'],
        'model_manifest_sha256':file_hash(run/'frozen-model.json'),
        'configuration_sha256':file_hash(run/'configuration.json')}
    write_pointer(output,'research-current.json',pointer)
    publish_desk(output)
    return {'status':'historical_experiment_completed','build':str(run),'rows':result['sampled_rows'],
        'folds':len(result['folds']),'fits':len(result['folds'])*len(plan['variants']),
        'execution_ready':False,'next':'update current observations and freeze research predictions'}


def read_build(output):
    pointer=read_json(Path(output)/'research-current.json')[0]; run=Path(pointer['build'])
    if Path(output).resolve() not in run.resolve().parents: raise ValueError('build outside workbench')
    if file_hash(run/'configuration.json')!=pointer['configuration_sha256'] or file_hash(run/'frozen-model.json')!=pointer['model_manifest_sha256']:
        raise ValueError('frozen build changed')
    model=read_json(run/'frozen-model.json')[0]
    if read_json(run/'dataset/dataset.json')[0]['source_sha256']!=file_hash(dataset.__file__):
        raise ValueError('feature formulas changed; build a new frozen model')
    if (model['model_id']!=identity({k:v for k,v in model.items() if k!='model_id'})
        or file_hash(model['model_path'])!=model['model_sha256']
        or file_hash(model['preprocessing_path'])!=model['preprocessing_sha256']):
        raise ValueError('frozen prediction model changed')
    return run,model,research.read_result(run/'experiment')


def predict(frame, calendar, model):
    import lightgbm as lgb
    calculated=dataset.features(frame,calendar)
    current=calculated[calculated.datetime==pd.Timestamp(calendar[-1])].copy()
    prep=read_json(model['preprocessing_path'])[0]; columns=prep['used_features']
    good=current.feature_eligible & np.isfinite(current[columns]).all(axis=1)
    current['prediction']=np.nan
    current['contributions']=None
    if good.any():
        booster=lgb.Booster(model_file=model['model_path'])
        current.loc[good,'prediction']=booster.predict(current.loc[good,columns],num_threads=2)
        contributions=booster.predict(current.loc[good,columns],pred_contrib=True,num_threads=2)
        for index,values in zip(current.index[good],contributions):
            current.at[index,'contributions']={**dict(zip(columns,map(float,values[:-1]))),'base_value':float(values[-1])}
    return current


def update(root, output, *, receipts=None, client=None):
    from tools.v2 import research_campaign as campaign
    run,model,_=read_build(output); config=read_json(run/'configuration.json')[0]
    moment=now_utc(); today=moment.astimezone(campaign.CST).date()
    if moment.astimezone(campaign.CST).hour<16: raise ValueError('post-close workbench update requires 16:00 Shanghai or later')
    folder=Path(output)/'observations'/uuid.uuid4().hex; folder.mkdir(parents=True)
    capture={'codes':[c+('.SH' if c.startswith('6') else '.SZ') for c in config['universe']],
        'start':(today-timedelta(days=89)).isoformat(),'end':today.isoformat(),
        'window_days':90,'max_requests':300,'selection_scope':'same_registered_research_universe_not_limit_pool_or_full_market'}
    write_json(folder/'request-plan.json',capture)
    source=Path(receipts) if receipts else folder/'receipts'
    if not receipts: campaign.capture(source,client=client,config=capture)
    # Replay the raw capture; it is not enough that files happen to exist.
    reg,_,_,_=campaign.replay(source)
    if reg.get('config')!=capture: raise ValueError('current update requires exact current range and frozen universe')
    observed=campaign.derive(source)
    calendar=observed['calendar']['SSE']
    if calendar!=observed['calendar']['SZSE'] or not calendar or calendar[-1]!=today.isoformat():
        raise ValueError('current exchange session not available; retain previous publication')
    frame=pd.DataFrame(observed['rows']); current=predict(frame,calendar,model)
    rows=[]
    for r in current.to_dict('records'):
        from collections import Counter
        score=float(r['prediction']) if pd.notna(r['prediction']) else None
        code=r['instrument']; original=next(x for x in observed['rows'] if x['instrument']==code and x['datetime']==calendar[-1])
        rows.append({'instrument':code,'date':calendar[-1],'prediction':score,
            'baseline_momentum_20d':float(r['ret_20d']) if pd.notna(r['ret_20d']) else None,
            'close_adjusted':float(r['close']) if pd.notna(r['close']) else None,
            'features':{k:float(r[k]) if pd.notna(r[k]) else None for k in dataset.BASE+dataset.MONEY},
            'contributions':r['contributions'],
            'status':'research_prediction' if score is not None else 'insufficient_current_features',
            'risks':['historical_availability_selected_training_universe','unvalidated_model_not_execution',
                'price_target_not_portfolio_return','model_age_and_market_regime_shift'],
            'source_gaps':original['gaps'],'receipt_files':original['receipt_files']})
        rows[-1]['window_gap_counts']=dict(Counter(gap for item in observed['rows'] if item['instrument']==code for gap in item['gaps']))
    rows.sort(key=lambda x:(x['prediction'] is None,-(x['prediction'] or 0),x['instrument']))
    result={'date':calendar[-1],'captured_at':now_utc().isoformat(),'model_id':model['model_id'],
        'model_frozen_at':model['frozen_at'],'model_train_end':model['train_end'],
        'scope':'actually_received_current_research_predictions_not_trading_signals',
        'receipt_manifest_id':observed['receipt_manifest_id'],'receipt_folder':str(source.resolve()),
        'rows':rows,'predictions':sum(r['prediction'] is not None for r in rows),'execution_ready':False,
        'feature_source_sha256':file_hash(dataset.__file__)}
    from .research_followup import collect
    result['reviews']=collect(output,frame,calendar,result['captured_at'])
    result['prediction_id']=identity(result)
    write_json(folder/'predictions.json',result)
    if not result['predictions']: raise ValueError('no nonempty current prediction; previous publication retained')
    write_pointer(output,'prediction-current.json',{'path':str(folder/'predictions.json'),'sha256':file_hash(folder/'predictions.json')})
    publish_desk(output)
    return {'date':result['date'],'predictions':result['predictions'],'cohort':len(rows),'prediction_id':result['prediction_id'],'execution_ready':False}


def read_prediction(output):
    pointer=Path(output)/'prediction-current.json'
    if not pointer.exists(): return None
    p=read_json(pointer)[0]; path=Path(p['path'])
    if Path(output).resolve() not in path.resolve().parents or file_hash(path)!=p['sha256']: raise ValueError('prediction artifact changed')
    result=read_json(path)[0]
    if result['prediction_id']!=identity({k:v for k,v in result.items() if k!='prediction_id'}): raise ValueError('prediction identity changed')
    return result


def publish_desk(output):
    from trade_system.file_lock import FileLock
    with FileLock(Path(output)/'publication.guard'):
        return _publish_desk(output)


def _publish_desk(output):
    from .research_product_view import render
    run,model,result=read_build(output)
    data={'research':result,'model':model,'prediction':read_prediction(output),
        'dataset':read_json(run/'dataset/dataset.json')[0],'notes':[],
        'scope':'local_research_product_no_execution','execution_ready':False}
    notes=Path(output)/'notes'
    if notes.exists():
        import heapq
        recent=heapq.nlargest(100,notes.glob('*.json'),key=lambda p:p.stat().st_mtime_ns)
        data['notes']=sorted([read_json(p)[0] for p in recent],key=lambda n:n['received_at'])
    data['reviews']=data['prediction'].get('reviews',[]) if data['prediction'] else []
    data['report_id']=identity(data)
    publication=Path(output)/'publication'
    generation=read_current(publication)[0]['generation']+1 if (publication/'current.json').exists() else 1
    return publish(publication,uuid.uuid4().hex,{'index.html':render(data).encode('utf-8'),'desk.json':canonical(data).encode()},generation=generation)


def save_note(output, values):
    prediction=read_prediction(output)
    if not prediction or values.get('prediction_id')!=prediction['prediction_id']: raise ValueError('note must bind current frozen prediction')
    if values.get('instrument') not in {r['instrument'] for r in prediction['rows']}: raise ValueError('unknown instrument')
    if values.get('intent') not in ('observe','reject','paper_hypothesis'): raise ValueError('non-executable intent required')
    for name in ('operator','hypothesis','invalidation'):
        if not isinstance(values.get(name),str) or not 1<=len(values[name].strip())<=4000: raise ValueError('explicit bounded human judgement required')
    note={k:values[k] for k in ('prediction_id','instrument','intent','operator','hypothesis','invalidation')}
    note.update(received_at=now_utc().isoformat(),identity_scope='caller_declared_not_authenticated',execution_ready=False)
    note['note_id']=identity(note); folder=Path(output)/'notes';folder.mkdir(exist_ok=True)
    write_json(folder/(note['note_id']+'.json'),note); publish_desk(output)
    return note['note_id']


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['build','update','serve','status'])
    p.add_argument('--config',default='config/research_delivery.json');p.add_argument('--output',default='reports/research-delivery')
    p.add_argument('--receipts');p.add_argument('--port',type=int,default=8766);p.add_argument('--open-browser',action='store_true')
    a=p.parse_args();root=Path(__file__).resolve().parents[2];output=(root/a.output).resolve()
    if a.command=='build': result=build(root/a.config,root,output)
    elif a.command=='update': result=update(root,output,receipts=a.receipts)
    elif a.command=='serve':
        from .research_product_server import serve
        return serve(root,output,a.port,open_browser=a.open_browser)
    else:
        _,model,result=read_build(output);prediction=read_prediction(output)
        result={'model_id':model['model_id'],'folds':len(result['folds']),'prediction_date':prediction['date'] if prediction else None,
            'predictions':prediction['predictions'] if prediction else 0,'execution_ready':False}
    print(canonical(result))
