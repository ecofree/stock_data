"""Local research delivery: existing dataset, QLib runner, observations and journal."""
import argparse
from datetime import timedelta
from pathlib import Path
from collections import Counter
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
    recent = set(config['sources']) == {'receipts'}
    previous = read_build(output)[1] if recent else None
    if recent:
        write_json(run/'previous-build-pointer.json',read_json(output/'research-current.json')[0])
    if recent:
        from . import research_recent
        meta=research_recent.build_dataset(config,root,run/'dataset')
    else:
        meta=dataset.build(config,root,run/'dataset')
    plan=dataset.experiment_plan(config,meta,'delivery-'+run.name[:12])
    research.register_experiment(run,run/'dataset/features.parquet',dict(plan,experiment_id='experiment'))
    result=research.run_experiment(run/'experiment')
    research.read_result(run/'experiment')
    from . import research_baselines
    rules=research_baselines.build(run,result)
    last=run/'experiment/run'/f"fold-{len(result['folds'])-1:02d}"/'price_baseline'
    model={'model_path':str(last/'model.txt'),'model_sha256':file_hash(last/'model.txt'),
        'preprocessing_path':str(last/'preprocessing.json'),'preprocessing_sha256':file_hash(last/'preprocessing.json'),
        'selection':'predeclared_last_fold_price_baseline_not_best_test_variant',
        'inference_policy':'price_21_sessions_v2',
        'train_end':result['folds'][-1]['partition']['train_end'],
        'validation_end':result['folds'][-1]['partition']['valid_end'],
        'frozen_at':now_utc().isoformat(),'execution_ready':False}
    if recent:
        research_recent.compare_previous(run,previous,config,result,plan)
        last,boundaries=research_recent.refit(run,config,result,plan)
        model.update(model_path=str(last/'model.txt'),model_sha256=file_hash(last/'model.txt'),
            preprocessing_path=str(last/'preprocessing.json'),preprocessing_sha256=file_hash(last/'preprocessing.json'),
            selection='predeclared_recent_price_refit_not_test_winner',
            train_end=boundaries['train_end'],validation_end=boundaries['validation_end'],
            refit_boundaries=boundaries,final_refit_scored=False,previous_model_id=previous['model_id'],
            frozen_at=now_utc().isoformat())
        frame,days,_=research_recent.load_receipts(config,root)
        current=predict(frame,days,model)
        if not current.prediction.notna().any():
            raise ValueError('recent model has no nonempty latest prediction; retain previous build')
        write_json(run/'inference-check.json',{'date':days[-1],
            'nonempty':int(current.prediction.notna().sum()),'execution_ready':False})
        previous_prediction=read_prediction(output)
        daily=read_json(run/'experiment/run/daily_metrics.json')[0]['price_baseline']
        readiness=research_recent.publication_readiness(daily,plan['top_k'],
            int(current.prediction.notna().sum()),previous_prediction['predictions'] if previous_prediction else 0)
        write_json(run/'publication-readiness.json',readiness)
    model['model_id']=identity(model)
    write_json(run/'frozen-model.json',model)
    if recent:
        write_json(run/'candidate-predictions.json',{'date':days[-1],'model_id':model['model_id'],
            'frozen_at':now_utc().isoformat(),'scope':'recent_model_candidate_not_active_prediction_archive',
            'rows':[{'instrument':r['instrument'],'prediction':float(r['prediction']) if pd.notna(r['prediction']) else None}
                for r in current.to_dict('records')], 'execution_ready':False})
    pointer={'build':str(run),'dataset_id':meta['dataset_id'],'model_id':model['model_id'],
        'model_manifest_sha256':file_hash(run/'frozen-model.json'),
        'configuration_sha256':file_hash(run/'configuration.json')}
    pointer['rule_baselines_sha256']=file_hash(run/'rule-baselines.json')
    pointer['rule_baselines_id']=rules['baseline_id']
    if recent:
        pointer['previous_comparison_sha256']=file_hash(run/'previous-model-comparison.json')
        pointer['readiness_sha256']=file_hash(run/'publication-readiness.json')
        publish_state(output,'research-candidate.json',pointer)
    if not recent or readiness['can_replace_current_model']:
        publish_state(output,'research-current.json',pointer)
    return {'status':'recent_candidate_not_promoted' if recent and not readiness['can_replace_current_model'] else 'historical_experiment_completed','build':str(run),'rows':result['sampled_rows'],
        'folds':len(result['folds']),'fits':len(result['folds'])*len(plan['variants'])+int(recent),
        'execution_ready':False,'next':'update current observations and freeze research predictions'}


def read_build(output, *, pointer_name='research-current.json'):
    if pointer_name not in ('research-current.json','research-candidate.json'):
        raise ValueError('known build pointer required')
    pointer=read_json(Path(output)/pointer_name)[0]; run=Path(pointer['build'])
    if Path(output).resolve() not in run.resolve().parents: raise ValueError('build outside workbench')
    if file_hash(run/'configuration.json')!=pointer['configuration_sha256'] or file_hash(run/'frozen-model.json')!=pointer['model_manifest_sha256']:
        raise ValueError('frozen build changed')
    if pointer.get('rule_baselines_sha256') and file_hash(run/'rule-baselines.json')!=pointer['rule_baselines_sha256']:
        raise ValueError('frozen rule baseline changed')
    model=read_json(run/'frozen-model.json')[0]
    meta=read_json(run/'dataset/dataset.json')[0]
    if meta['source_sha256']!=file_hash(dataset.__file__):
        raise ValueError('feature formulas changed; build a new frozen model')
    if meta.get('recent_source_sha256'):
        from . import research_recent
        if meta['recent_source_sha256']!=file_hash(research_recent.__file__):
            raise ValueError('recent dataset source changed; rebuild required')
        if file_hash(run/'previous-model-comparison.json')!=pointer.get('previous_comparison_sha256'):
            raise ValueError('previous model comparison changed')
        if file_hash(run/'publication-readiness.json')!=pointer.get('readiness_sha256'):
            raise ValueError('model readiness changed')
        comparison=read_json(run/'previous-model-comparison.json')[0]
        for relative,expected in comparison['prediction_files'].items():
            path=(run/relative).resolve()
            if run.resolve() not in path.parents or file_hash(path)!=expected:
                raise ValueError('previous model predictions changed')
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
    policy=model.get('inference_policy','legacy_61_sessions')
    if policy not in ('legacy_61_sessions','price_21_sessions_v2'):raise ValueError('unknown frozen inference policy')
    good=current['price_eligible' if policy=='price_21_sessions_v2' else 'feature_eligible'] & np.isfinite(current[columns]).all(axis=1)
    current['prediction']=np.nan
    current['contributions']=None
    if good.any():
        booster=lgb.Booster(model_file=model['model_path'])
        current.loc[good,'prediction']=booster.predict(current.loc[good,columns],num_threads=2)
        contributions=booster.predict(current.loc[good,columns],pred_contrib=True,num_threads=2)
        for index,values in zip(current.index[good],contributions):
            current.at[index,'contributions']={**dict(zip(columns,map(float,values[:-1]))),'base_value':float(values[-1])}
    return current


def latest_closed_session(calendar, moment):
    local=pd.Timestamp(moment).tz_convert('Asia/Shanghai')
    if list(calendar)!=sorted(set(calendar)):raise ValueError('ordered unique exchange calendar required')
    eligible=[d for d in calendar if pd.Timestamp(d+'T16:00:00',tz='Asia/Shanghai')<=local]
    if not eligible:raise ValueError('no closed exchange session in receipt window')
    return eligible[-1]


def forecast_timing(entry,captured_at):
    eligible=bool(pd.Timestamp(captured_at)<pd.Timestamp(entry)) if entry else None
    return {'prospective_eligible':eligible,'forecast_status':
        'pending_future_calendar' if eligible is None else 'before_entry' if eligible else 'after_entry'}


def update(root, output, *, receipts=None, client=None, replay_build=False):
    from tools.v2 import research_campaign as campaign
    run,model,_=read_build(output); config=read_json(run/'configuration.json')[0]
    if replay_build:
        from . import research_recent
        if receipts or client or set(config['sources'])!={'receipts'}:
            raise ValueError('build replay requires its own frozen receipt source')
        research_recent.load_receipts(config,root)
        receipts=Path(root)/config['sources']['receipts']['path']
    moment=now_utc(); today=moment.astimezone(campaign.CST).date()
    folder=Path(output)/'observations'/uuid.uuid4().hex; folder.mkdir(parents=True)
    capture={'codes':[c+('.SH' if c.startswith('6') else '.SZ') for c in config['universe']],
        'start':(today-timedelta(days=89)).isoformat(),'end':today.isoformat(),
        'window_days':90,'max_requests':300,'selection_scope':'same_registered_research_universe_not_limit_pool_or_full_market'}
    write_json(folder/'request-plan.json',capture)
    source=Path(receipts) if receipts else folder/'receipts'
    if not receipts: campaign.capture(source,client=client,config=capture)
    # Replay the raw capture; it is not enough that files happen to exist.
    reg,_,parsed,_=campaign.replay(source)
    if receipts:
        saved=reg.get('config') or {}
        if (saved.get('codes')!=capture['codes'] or (not replay_build and saved.get('selection_scope')!=capture['selection_scope'])
            or saved.get('end','')>today.isoformat()):raise ValueError('replay requires frozen universe and nonfuture receipt range')
    elif reg.get('config')!=capture: raise ValueError('current update requires exact current range and frozen universe')
    observed=campaign.derive(source)
    calendar=observed['calendar']['SSE']
    if calendar!=observed['calendar']['SZSE'] or not calendar:
        raise ValueError('current exchange session not available; retain previous publication')
    native_calendar=parsed.get(0,[])
    market_calendar=native_calendar or calendar
    if receipts and not replay_build and (not native_calendar or max(native_calendar)<today.isoformat()):
        raise ValueError('replay cannot certify latest market session')
    session=latest_closed_session(market_calendar,moment)
    if session not in calendar:raise ValueError('latest closed session absent from receipts; retain previous date')
    calendar=[d for d in calendar if d<=session]
    frame=pd.DataFrame([r for r in observed['rows'] if r['datetime']<=session]); current=predict(frame,calendar,model)
    future=[d for d in market_calendar if d>session]
    entry=future[0]+'T09:30:00+08:00' if future else None
    rows=[]
    for r in current.to_dict('records'):
        score=float(r['prediction']) if pd.notna(r['prediction']) else None
        code=r['instrument']; original=next(x for x in observed['rows'] if x['instrument']==code and x['datetime']==calendar[-1])
        rows.append({'instrument':code,'date':calendar[-1],'prediction':score,
            'name':observed['identity_snapshots'].get(code+('.SH' if code.startswith('6') else '.SZ'),{}).get('name',''),
            'baseline_momentum_20d':float(r['ret_20d']) if pd.notna(r['ret_20d']) else None,
            'close_adjusted':float(r['close']) if pd.notna(r['close']) else None,
            'features':{k:float(r[k]) if pd.notna(r[k]) else None for k in dataset.BASE+dataset.MONEY},
            'contributions':r['contributions'],
            'eligibility':{k:bool(r[k]) for k in ('price_eligible','money_eligible','alpha158_window_eligible')},
            'status':'research_prediction' if score is not None else 'insufficient_current_features',
            'risks':['historical_availability_selected_training_universe','unvalidated_model_not_execution',
                'price_target_not_portfolio_return','model_age_and_market_regime_shift'],
            'source_gaps':original['gaps'],'receipt_files':original['receipt_files']})
        history=[item for item in observed['rows'] if item['instrument']==code and item['datetime']<=session]
        rows[-1]['window_gap_counts']=dict(Counter(gap for item in history for gap in item['gaps']))
        rows[-1]['active_window_gap_counts']=dict(Counter(gap for item in history[-21:] for gap in item['gaps']))
    rows.sort(key=lambda x:(x['prediction'] is None,-(x['prediction'] or 0),x['instrument']))
    result={'date':calendar[-1],'captured_at':now_utc().isoformat(),'model_id':model['model_id'],
        'model_frozen_at':model['frozen_at'],'model_train_end':model['train_end'],
        'scope':'actually_received_current_research_predictions_not_trading_signals',
        'receipt_manifest_id':observed['receipt_manifest_id'],'receipt_folder':str(source.resolve()),
        'rows':rows,'predictions':sum(r['prediction'] is not None for r in rows),'execution_ready':False,
        'feature_source_sha256':file_hash(dataset.__file__)}
    result.update(scheduled_entry_at=entry,scheduled_exit_date=future[1] if len(future)>1 else None,
        **forecast_timing(entry,result['captured_at']),
        received_date=today.isoformat(),receipt_replay=bool(receipts),
        verified_calendar_scope='native_exchange_calendar_with_dual_relay_history',
        data_received_at=max(read_json(path)[0]['received_at'] for path in source.glob('receipt-*.json')),
        baseline_rule='descending 20-session adjusted return; ties by code; no training')
    if replay_build:
        result.update(scope='frozen_recent_receipt_replay_not_latest_session_certified',
            latest_session_certified=False)
    from .research_followup import collect
    result['reviews']=collect(output,frame,calendar,result['captured_at'])
    result['prediction_id']=identity(result)
    write_json(folder/'predictions.json',result)
    if not result['predictions']: raise ValueError('no nonempty current prediction; previous publication retained')
    publish_state(output,'prediction-current.json',{'path':str(folder/'predictions.json'),'sha256':file_hash(folder/'predictions.json')})
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


def publish_state(output,name,value):
    """Keep the previous state pointer if building/publishing its page fails."""
    from trade_system.file_lock import FileLock
    if name not in ('research-current.json','research-candidate.json','prediction-current.json'):raise ValueError('known state pointer required')
    path=Path(output)/name
    with FileLock(Path(output)/'publication.guard'):
        previous=read_json(path)[0] if path.exists() else None
        write_pointer(output,name,value)
        try:return _publish_desk(output)
        except Exception:
            if previous is not None:write_pointer(output,name,previous)
            else:path.unlink()
            raise


def _publish_desk(output):
    from .research_product_view import render
    run,model,result=read_build(output)
    candidate_model=None;readiness=None
    if (Path(output)/'research-candidate.json').exists():
        run,candidate_model,result=read_build(output,pointer_name='research-candidate.json')
        readiness=read_json(run/'publication-readiness.json')[0]
    data={'research':result,'model':model,'prediction':read_prediction(output),
        'dataset':read_json(run/'dataset/dataset.json')[0],'notes':[],
        'scope':'local_research_product_no_execution','execution_ready':False}
    data['candidate_model']=candidate_model
    data['candidate_readiness']=readiness
    notes=Path(output)/'notes'
    if notes.exists():
        import heapq
        recent=heapq.nlargest(100,notes.glob('*.json'),key=lambda p:p.stat().st_mtime_ns)
        data['notes']=sorted([read_json(p)[0] for p in recent],key=lambda n:n['received_at'])
    data['reviews']=data['prediction'].get('reviews',[]) if data['prediction'] else []
    from . import research_baselines, research_journal
    data['rule_baselines']=research_baselines.read(run) if (run/'rule-baselines.json').exists() else None
    data['previous_model_comparison']=read_json(run/'previous-model-comparison.json')[0] if (run/'previous-model-comparison.json').exists() else None
    data['notes']=research_journal.annotate(data['notes'])
    data['reviews']=research_journal.attach(data['reviews'],data['notes'])
    data['report_id']=identity(data)
    publication=Path(output)/'publication'
    generation=read_current(publication)[0]['generation']+1 if (publication/'current.json').exists() else 1
    return publish(publication,uuid.uuid4().hex,{'index.html':render(data).encode('utf-8'),'desk.json':canonical(data).encode()},generation=generation)


def save_note(output, values):
    from . import research_journal
    prediction=read_prediction(output)
    if prediction and values.get('prediction_id')!=prediction['prediction_id']:
        prediction=research_journal.find_prediction(output,values.get('prediction_id'))
    if not prediction or values.get('prediction_id')!=prediction['prediction_id']: raise ValueError('note must bind current frozen prediction')
    if values.get('instrument') not in {r['instrument'] for r in prediction['rows']}: raise ValueError('unknown instrument')
    if values.get('intent') not in ('observe','reject','paper_hypothesis'): raise ValueError('non-executable intent required')
    for name in ('operator','hypothesis','invalidation'):
        if not isinstance(values.get(name),str) or not 1<=len(values[name].strip())<=4000: raise ValueError('explicit bounded human judgement required')
    note={k:values[k] for k in ('prediction_id','instrument','intent','operator','hypothesis','invalidation')}
    note.update(received_at=now_utc().isoformat(),identity_scope='caller_declared_not_authenticated',execution_ready=False)
    research_journal.bind(output,note,prediction,values.get('supersedes',''))
    note['note_id']=identity(note); folder=Path(output)/'notes';folder.mkdir(exist_ok=True)
    write_json(folder/(note['note_id']+'.json'),note); publish_desk(output)
    return note['note_id']


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['build','update','serve','status'])
    p.add_argument('--config',default='config/research_delivery.json');p.add_argument('--output',default='reports/research-delivery')
    p.add_argument('--receipts');p.add_argument('--replay-build',action='store_true',help='Replay only the frozen training receipts; does not certify the latest market date')
    p.add_argument('--port',type=int,default=8766);p.add_argument('--open-browser',action='store_true')
    a=p.parse_args();root=Path(__file__).resolve().parents[2];output=(root/a.output).resolve()
    if a.command=='build': result=build(root/a.config,root,output)
    elif a.command=='update': result=update(root,output,receipts=a.receipts,replay_build=a.replay_build)
    elif a.command=='serve':
        from .research_product_server import serve
        return serve(root,output,a.port,open_browser=a.open_browser)
    else:
        _,model,result=read_build(output);prediction=read_prediction(output)
        result={'model_id':model['model_id'],'folds':len(result['folds']),'prediction_date':prediction['date'] if prediction else None,
            'predictions':prediction['predictions'] if prediction else 0,'execution_ready':False}
        result['active_train_end']=model['train_end']
        if (output/'research-candidate.json').exists():
            candidate_run,candidate,_=read_build(output,pointer_name='research-candidate.json')
            result['candidate_train_end']=candidate['train_end']
            result['candidate_readiness']=read_json(candidate_run/'publication-readiness.json')[0]
    print(canonical(result))
