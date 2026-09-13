"""Local research delivery: existing dataset, QLib runner, observations and journal."""
import argparse
from datetime import timedelta
from pathlib import Path
from collections import Counter
import uuid

from .domain import canonical, file_hash, identity, now_utc
from .gap_evidence import read_json, write_json
from .publisher import publish, read_current


def write_pointer(root, name, value):
    temp=Path(root)/('.'+name+'-'+uuid.uuid4().hex)
    write_json(temp,value); temp.replace(Path(root)/name)


def build(config_path, root, output):
    import pandas as pd
    from . import research_dataset as dataset, rolling_research as research
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
        write_json(run/'capacity-preflight.json',research_recent.capacity_preflight(config,root))
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


def read_build(output, *, pointer_name='research-current.json', historical=False):
    from . import research_dataset as dataset, rolling_research as research
    if pointer_name not in ('research-current.json','research-candidate.json'):
        raise ValueError('known build pointer required')
    pointer=read_json(Path(output)/pointer_name)[0]; run=Path(pointer['build'])
    if Path(output).resolve() not in run.resolve().parents: raise ValueError('build outside workbench')
    if file_hash(run/'configuration.json')!=pointer['configuration_sha256'] or file_hash(run/'frozen-model.json')!=pointer['model_manifest_sha256']:
        raise ValueError('frozen build changed')
    if pointer.get('rule_baselines_sha256') and file_hash(run/'rule-baselines.json')!=pointer['rule_baselines_sha256']:
        raise ValueError('frozen rule baseline changed')
    model=read_json(run/'frozen-model.json')[0]
    meta=dataset_metadata(run)
    if not historical and meta['source_sha256']!=file_hash(dataset.__file__) and not dataset.inference_compatible(meta):
        raise ValueError('feature formulas changed; build a new frozen model')
    if meta.get('recent_source_sha256'):
        from . import research_recent
        if not historical and meta['recent_source_sha256']!=file_hash(research_recent.__file__) and not dataset.inference_compatible(meta):
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


def dataset_metadata(run):
    folder=Path(run)/'dataset'
    canonical_path=folder/'features.metadata.json'
    legacy_path=folder/'dataset.json'
    if canonical_path.exists():
        value=read_json(canonical_path)[0]
        if legacy_path.exists() and read_json(legacy_path)[0]!=value:
            raise ValueError('conflicting legacy and canonical dataset metadata')
        return value
    return read_json(legacy_path)[0]


def predict(frame, calendar, model):
    import numpy as np
    import pandas as pd
    from . import research_dataset as dataset
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
    import pandas as pd
    local=pd.Timestamp(moment).tz_convert('Asia/Shanghai')
    if list(calendar)!=sorted(set(calendar)):raise ValueError('ordered unique exchange calendar required')
    eligible=[d for d in calendar if pd.Timestamp(d+'T16:00:00',tz='Asia/Shanghai')<=local]
    if not eligible:raise ValueError('no closed exchange session in receipt window')
    return eligible[-1]


def forecast_timing(entry,captured_at):
    import pandas as pd
    eligible=bool(pd.Timestamp(captured_at)<pd.Timestamp(entry)) if entry else None
    return {'prospective_eligible':eligible,'forecast_status':
        'pending_future_calendar' if eligible is None else 'before_entry' if eligible else 'after_entry'}


def update(root, output, *, receipts=None, client=None, replay_build=False, force_refresh=False):
    """All callers share one update owner, including CLI and HTTP."""
    from trade_system.file_lock import FileLock
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    with FileLock(output/'update.guard'):
        return _update(root,output,receipts=receipts,client=client,replay_build=replay_build,force_refresh=force_refresh)


def configured_market(output, prediction):
    path=Path(output)/'workspace-config.json'
    if not path.exists():return None
    from .market_workspace import latest_snapshot
    config=read_json(path)[0]
    return latest_snapshot(config['market_database'],now_utc().isoformat(),
                    [r['instrument'] for r in prediction['rows']])


def calendar_covers_clock(reg, parsed, today):
    """Native sessions or two exact exchange day flags, never a weekday guess."""
    native=parsed.get(0,[])
    if native and max(native)>=today:return True
    flags={}
    for index,request in enumerate(reg.get('requests',[])):
        if request.get('kind')!='calendar':continue
        exchange=request['params'].get('exchange')
        records=[r for r in parsed.get(index,[]) if r['cal_date']==today.replace('-','')]
        if len(records)==1:flags[exchange]=records[0]['is_open']
    # A closed day is certified by explicit matching exchange responses. An
    # open day missing from native calendar still requires a refresh.
    return flags=={'SSE':0,'SZSE':0} and bool(native)


def _update(root, output, *, receipts=None, client=None, replay_build=False, force_refresh=False):
    import pandas as pd
    from . import research_dataset as dataset
    from . import research_campaign as campaign
    run,model,_=read_build(output); config=read_json(run/'configuration.json')[0]
    if replay_build:
        from . import research_recent
        if receipts or client or set(config['sources'])!={'receipts'}:
            raise ValueError('build replay requires its own frozen receipt source')
        research_recent.load_receipts(config,root)
        receipts=Path(root)/config['sources']['receipts']['path']
    moment=now_utc(); today=moment.astimezone(campaign.CST).date()
    capture={'codes':[c+('.SH' if c.startswith('6') else '.SZ') for c in config['universe']],
        'start':(today-timedelta(days=89)).isoformat(),'end':today.isoformat(),
        'window_days':90,'max_requests':300,'selection_scope':'same_registered_research_universe_not_limit_pool_or_full_market'}
    base=None;replay_cache={}
    prior=read_prediction(output)
    if not receipts and prior and prior.get('receipt_folder'):
        previous=Path(prior['receipt_folder'])
        old,members,parsed_prior,_=campaign.cached_replay(previous,replay_cache)
        saved=old.get('config') or {}
        previous_calendar=parsed_prior.get(0,[])
        same_model=prior.get('model_id')==model['model_id']
        if (not force_refresh and same_model and saved.get('codes')==capture['codes']
            and saved.get('selection_scope')==capture['selection_scope']
            and old['origin']=='native_and_relay' and previous_calendar and calendar_covers_clock(old,parsed_prior,today.isoformat())
            and latest_closed_session(previous_calendar,moment)==prior['date']):
            campaign.derive(previous,replayed=replay_cache[str(previous.resolve())],replay_cache=replay_cache)
            market=configured_market(output,prior)
            if market is not None:publish_desk(output,market=market)
            return {'status':'sealed_session_reused','date':prior['date'],'predictions':prior['predictions'],
                    'prediction_id':prior['prediction_id'],'provider_requests':0,'fits':0,'execution_ready':False}
        elapsed=(today-pd.Timestamp(saved.get('end',today)).date()).days
        if (saved.get('codes')==capture['codes'] and saved.get('selection_scope')==capture['selection_scope']
            and 0<=elapsed<=7 and old.get('incremental_depth',0)<6):
            base=previous
            capture['start']=max(saved['start'],(pd.Timestamp(saved['end']).date()-timedelta(days=4)).isoformat())
    folder=Path(output)/'observations'/uuid.uuid4().hex; folder.mkdir(parents=True)
    write_json(folder/'request-plan.json',capture)
    source=Path(receipts) if receipts else folder/'receipts'
    if not receipts: campaign.capture(source,client=client,config=capture,base=base,replay_cache=replay_cache)
    # Replay the raw capture; it is not enough that files happen to exist.
    replayed=campaign.cached_replay(source,replay_cache)
    reg,_,parsed,_=replayed
    if receipts:
        saved=reg.get('config') or {}
        if (saved.get('codes')!=capture['codes'] or (not replay_build and saved.get('selection_scope')!=capture['selection_scope'])
            or saved.get('end','')>today.isoformat()):raise ValueError('replay requires frozen universe and nonfuture receipt range')
    elif reg.get('config')!=capture: raise ValueError('current update requires exact current range and frozen universe')
    observed=campaign.derive(source,replayed=replayed,replay_cache=replay_cache)
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
    market=configured_market(output,result)
    state={'path':str(folder/'predictions.json'),'sha256':file_hash(folder/'predictions.json')}
    if market is None:publish_state(output,'prediction-current.json',state)
    else:publish_state(output,'prediction-current.json',state,market=market)
    return {'date':result['date'],'predictions':result['predictions'],'cohort':len(rows),'prediction_id':result['prediction_id'],
            'update_mode':'receipt_replay' if receipts else 'incremental_revision' if base else 'bounded_bootstrap_checkpoint',
            'provider_requests':0 if receipts else len(campaign.plan(capture)),'execution_ready':False}


def read_prediction(output):
    pointer=Path(output)/'prediction-current.json'
    if not pointer.exists(): return None
    p=read_json(pointer)[0]; path=Path(p['path'])
    if Path(output).resolve() not in path.resolve().parents or file_hash(path)!=p['sha256']: raise ValueError('prediction artifact changed')
    result=read_json(path)[0]
    if result['prediction_id']!=identity({k:v for k,v in result.items() if k!='prediction_id'}): raise ValueError('prediction identity changed')
    return result


def publish_desk(output,*,market=None):
    from trade_system.file_lock import FileLock
    with FileLock(Path(output)/'publication.guard'):
        return _publish_desk(output) if market is None else _publish_desk(output,market=market)


def publish_state(output,name,value,*,market=None):
    """Keep the previous state pointer if building/publishing its page fails."""
    from trade_system.file_lock import FileLock
    if name not in ('research-current.json','research-candidate.json','prediction-current.json','price-study-current.json'):raise ValueError('known state pointer required')
    path=Path(output)/name
    with FileLock(Path(output)/'publication.guard'):
        previous=read_json(path)[0] if path.exists() else None
        write_pointer(output,name,value)
        try:return _publish_desk(output) if market is None else _publish_desk(output,market=market)
        except Exception:
            if previous is not None:write_pointer(output,name,previous)
            else:path.unlink()
            raise


def _publish_desk(output,*,market=None):
    from .daily_workspace import projection
    from .research_product_view import render
    data=projection(output,market=market,research_loader=_research_projection)
    publication=Path(output)/'publication'
    generation=read_current(publication)[0]['generation']+1 if (publication/'current.json').exists() else 1
    return publish(publication,uuid.uuid4().hex,{'index.html':render(data).encode('utf-8'),'desk.json':canonical(data).encode()},generation=generation)


def _research_projection(output):
    """Optional frozen research evidence; never the owner of a market session."""
    run,model,result=read_build(output,historical=True)
    candidate_model=None;readiness=None
    if (Path(output)/'research-candidate.json').exists():
        # Historical display is not permission to use changed formulas for inference.
        run,candidate_model,result=read_build(output,pointer_name='research-candidate.json',historical=True)
        readiness=read_json(run/'publication-readiness.json')[0]
    data={'research':result,'model':model,'prediction':read_prediction(output),
        'dataset':dataset_metadata(run),'notes':[],
        'scope':'local_research_product_no_execution','execution_ready':False}
    data['candidate_model']=candidate_model
    data['candidate_readiness']=readiness
    data['reviews']=data['prediction'].get('reviews',[]) if data['prediction'] else []
    from . import research_baselines
    data['rule_baselines']=research_baselines.read(run) if (run/'rule-baselines.json').exists() else None
    data['previous_model_comparison']=read_json(run/'previous-model-comparison.json')[0] if (run/'previous-model-comparison.json').exists() else None
    from . import price_study
    data['price_study']=price_study.read(output)
    return data


def journal_projection(output,data):
    """Read-only projection; no model loading, fetching, fitting or business writes."""
    from . import research_journal
    from copy import deepcopy
    import heapq
    data=deepcopy(data);data.pop('report_id',None)
    paths=research_journal.note_paths(output)
    recent=heapq.nlargest(100,paths,key=lambda p:p.stat().st_mtime_ns)
    notes=sorted([read_json(p)[0] for p in recent],key=lambda n:n['received_at'])
    data['notes']=research_journal.annotate(notes)
    data['attention_followups']=research_journal.attention_followups(output,data['notes'],data.get('market'))
    review_paths=heapq.nlargest(100,research_journal.review_paths(output),key=lambda p:p.stat().st_mtime_ns)
    data['human_reviews']=[research_journal.read_review(output,p.stem) for p in reversed(review_paths)]
    data['note_scope']={'shown':len(notes),'total':sum(1 for _ in research_journal.note_paths(output)),'limit':100}
    reviews=data.setdefault('reviews',[])
    represented={r['prediction_id'] for r in reviews}
    for note in data['notes']:
        if not note.get('prediction_id'):continue
        if note['prediction_id'] in represented:continue
        frozen=data.get('prediction')
        if not frozen or frozen['prediction_id']!=note['prediction_id']:
            frozen=research_journal.find_prediction(output,note['prediction_id'])
        if frozen['prediction_id']!=identity({k:v for k,v in frozen.items() if k!='prediction_id'}):
            raise ValueError('judged frozen prediction changed')
        # Reading/saving a note never evaluates future labels. Keep its complete
        # original queue visible until the shared update service evaluates it.
        reviews.append({'prediction_id':frozen['prediction_id'],'prediction_date':frozen['date'],
            'status':'awaiting_review_refresh','paired':0,'count_in_summary':False,
            'rows':[dict(instrument=r['instrument'],prediction=r['prediction'],
                target_pct=None,error_pct=None,entry_date=None,exit_date=None,
                status='awaiting_review_refresh') for r in frozen['rows']]})
        represented.add(note['prediction_id'])
    for review in data.get('reviews',[]):
        if not review.get('rows') and review.get('status') in ('pending_exact_future_sessions','outside_current_receipt_window','not_prospective'):
            frozen=research_journal.find_prediction(output,review['prediction_id'])
            review['rows']=[{'instrument':r['instrument'],'prediction':r['prediction'],
                'target_pct':None,'error_pct':None,'entry_date':None,'exit_date':None,
                'status':review['status'],'baseline_momentum_20d':r.get('baseline_momentum_20d')}
                for r in frozen['rows']]
    data['reviews']=research_journal.attach(data.get('reviews',[]),data['notes'])
    observation=Path(output)/'observation-publication'
    if (observation/'current.json').exists():
        import json
        from .observation_workspace import present as present_observation
        try:
            _,files=read_current(observation)
            data['observation']=present_observation(json.loads(files['observation.json']),now_utc().isoformat())
        except (ValueError,KeyError,OSError):
            # A failed optional quote projection disables quotes, not saved
            # judgements or the independently sealed daily product.
            data['observation']={'error':'observation_snapshot_unavailable','qualified':0,
                                 'rows':[],'as_of':None,'execution_ready':False}
    from .operator_workflow import observation_plans
    try:
        data['plans']=observation_plans(output,now_utc().isoformat(),(data.get('observation') or {}).get('rows',[]))
    except (OSError,ValueError,KeyError) as exc:
        data['plans']={'rows':[],'account':{'status':'risk_unavailable','execution_ready':False},'error':str(exc)[:200]}
    data['report_id']=identity(data)
    return data


def observe(output,*,capture_quotes=False,quote_receipts=None):
    """One update owner for retained quotes, explicit bounded capture or replay."""
    from trade_system.file_lock import FileLock
    from .observation_workspace import load_rows,project_rows
    from . import observation_capture
    output=Path(output)
    with FileLock(output/'update.guard'):
        if capture_quotes and quote_receipts:raise ValueError('capture and replay are mutually exclusive')
        config=read_json(output/'workspace-config.json')[0]
        if config.get('read_only') is not True:raise ValueError('explicit read-only market source required')
        data=saved_projection(output)
        codes={r['instrument'] for r in (data.get('prediction') or {}).get('rows',[])}
        codes.update(n['instrument'] for n in data['notes'] if n['is_latest'] and n['intent']=='observe')
        clock=now_utc();rows=load_rows(config['market_database'],codes,clock.isoformat())
        value=project_rows(rows,codes,clock.isoformat());acquired=None;requests=0;reused=False
        missing=[r['instrument'] for r in value['rows'] if r['state']=='no_qualified_current_quote']
        if quote_receipts:
            acquired=observation_capture.replay(quote_receipts)
            if acquired['origin']!='tencent_https' or not set(acquired['codes'])<=codes:
                raise ValueError('real same-universe quote replay required')
        elif capture_quotes and missing:
            pointer=output/'quote-capture-current.json'
            if pointer.exists():
                cached=read_json(pointer)[0]
                from .domain import utc
                age=(clock-utc(cached['captured_at'])).total_seconds()
                if cached['codes']==missing and 0<=age<60:
                    acquired=observation_capture.replay(cached['folder'])
                    if acquired['manifest_id']!=cached['manifest_id'] or acquired['origin']!='tencent_https':
                        raise ValueError('quote capture cache identity differs')
                    reused=True
            if acquired is None:
                acquired=observation_capture.capture(output/'quote-captures'/uuid.uuid4().hex,missing)
                requests=acquired['requests']
                write_pointer(output,'quote-capture-current.json',{'captured_at':now_utc().isoformat(),
                    'codes':missing,'folder':acquired['folder'],'manifest_id':acquired['manifest_id']})
        if acquired is None and not quote_receipts and (output/'quote-capture-current.json').exists():
            cached=read_json(output/'quote-capture-current.json')[0]
            acquired=observation_capture.replay(cached['folder'])
            if (acquired['manifest_id']!=cached['manifest_id'] or acquired['origin']!='tencent_https'
                or not set(acquired['codes'])<=codes):
                raise ValueError('retained quote receipt identity/universe differs')
            reused=True
        if acquired:
            value=project_rows([*rows,*acquired['rows']],codes,now_utc().isoformat())
            value.pop('snapshot_id')
            value['capture']={k:v for k,v in acquired.items() if k not in ('rows','codes')}
            value['capture'].update(provider_requests_this_run=requests,reused=reused,
                                    received_rows=len(acquired['rows']))
            value['snapshot_id']=identity(value)
        destination=output/'observation-publication'
        generation=read_current(destination)[0]['generation']+1 if (destination/'current.json').exists() else 1
        publish(destination,uuid.uuid4().hex,{'observation.json':canonical(value).encode()},generation=generation)
        return {'snapshot_id':value['snapshot_id'],'qualified':value['qualified'],'securities':len(value['rows']),
                'provider_requests':requests,'received_rows':len(acquired['rows']) if acquired else 0,
                'capture_reused':reused,'capture_failures':acquired['failures'] if acquired else 0,
                'fits':0,'execution_ready':False}


def saved_projection(output,*,market_review=None):
    """Read a sealed projection without network/model execution."""
    import json
    _,files=read_current(Path(output)/'publication')
    data=json.loads(files['desk.json'])
    if data['report_id']!=identity({k:v for k,v in data.items() if k!='report_id'}):
        raise ValueError('published projection identity changed')
    data=journal_projection(output,data)
    if market_review:
        market=read_json(market_review)[0]
        if market.get('snapshot_id')!=identity({k:v for k,v in market.items() if k!='snapshot_id'}):
            raise ValueError('market snapshot identity changed')
        if market.get('scope')!='read_only_market_review_not_execution' or market.get('execution_ready') is not False:
            raise ValueError('non-executable market snapshot required')
        if not data.get('prediction') or market['trade_date']!=data['prediction']['date']:
            raise ValueError('market and prediction dates differ; do not silently combine')
        data['market']=market
    data.pop('report_id',None);data['report_id']=identity(data)
    return data


def present(output,*,market_review=None):
    """Replace the local research view, not its model/prediction or production tasks."""
    from trade_system.file_lock import FileLock
    from .research_product_view import render
    output=Path(output)
    with FileLock(output/'publication.guard'):
        data=saved_projection(output,market_review=market_review)
        publication=output/'publication'
        generation=read_current(publication)[0]['generation']+1
        return publish(publication,uuid.uuid4().hex,
            {'index.html':render(data).encode('utf-8'),'desk.json':canonical(data).encode()},generation=generation)


def render_saved(output,destination,*,market_review=None):
    """Export to a new directory without changing current publication."""
    from .research_product_view import render,export_projection
    data=export_projection(saved_projection(output,market_review=market_review))
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('new isolated render destination required')
    destination.mkdir(parents=True)
    receipt=publish(destination/'publication',uuid.uuid4().hex,
        {'index.html':render(data).encode('utf-8'),'desk.json':canonical(data).encode()},generation=1)
    return {'publication':receipt,'destination':str(destination),'provider_requests':0,'fits':0,
            'prediction_id':data['prediction']['prediction_id'] if data.get('prediction') else None,
            'execution_ready':False,'production_cutover':False}


def save_note(output, values):
    """Durable command acknowledgement is independent of page publication."""
    from trade_system.file_lock import FileLock
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with FileLock(output/'judgement.guard'):
        return _save_note(output,values)


def _save_note(output, values):
    if not values.get('prediction_id'):
        from .research_journal import save_attention
        return save_attention(output,values)
    from . import research_journal
    command={k:values.get(k) for k in ('prediction_id','instrument','intent','operator','hypothesis','invalidation')}
    command['supersedes']=values.get('supersedes') or None
    request_id=values.get('request_id')
    prior=research_journal.retry_note(output,request_id,command)
    if prior:return prior
    prediction=read_prediction(output)
    if prediction and values.get('prediction_id')!=prediction['prediction_id']:
        prediction=research_journal.find_prediction(output,values.get('prediction_id'))
    if not prediction or values.get('prediction_id')!=prediction['prediction_id']: raise ValueError('note must bind current frozen prediction')
    if values.get('instrument') not in {r['instrument'] for r in prediction['rows']}: raise ValueError('unknown instrument')
    if values.get('intent') not in ('observe','reject','paper_hypothesis'): raise ValueError('non-executable intent required')
    for name in ('operator','hypothesis','invalidation'):
        if not isinstance(values.get(name),str) or not 1<=len(values[name].strip())<=4000: raise ValueError('explicit bounded human judgement required')
    note={k:values[k] for k in ('prediction_id','instrument','intent','operator','hypothesis','invalidation')}
    if request_id:note.update(request_id=request_id,command_id=identity(command))
    note.update(received_at=now_utc().isoformat(),identity_scope='caller_declared_not_authenticated',execution_ready=False)
    research_journal.bind(output,note,prediction,values.get('supersedes',''))
    note['note_id']=identity(note)
    research_journal.append_note(output,note)
    return note['note_id']


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['build','update','market-update','observe','serve','stop','status','preflight','price-study','utility','render','present','configure-market'])
    p.add_argument('--config',default='config/research_delivery.json');p.add_argument('--output',default='reports/research-delivery')
    p.add_argument('--receipts');p.add_argument('--replay-build',action='store_true',help='Replay only the frozen training receipts; does not certify the latest market date')
    p.add_argument('--destination',help='New isolated destination for a read-only render')
    p.add_argument('--force-refresh',action='store_true',help='Explicitly recapture the bounded revision window instead of reusing the sealed session')
    p.add_argument('--market-review',help='Same-date immutable market review projection')
    p.add_argument('--market-db',help='Explicit read-only canonical database for daily workspace')
    p.add_argument('--donor',help='Sealed receipt batch supplying exactly one failed factor request')
    p.add_argument('--capture-quotes',action='store_true',help='Explicit bounded Tencent fallback observation, not execution')
    p.add_argument('--quote-receipts',help='Replay a sealed real quote receipt folder without network')
    p.add_argument('--port',type=int,default=8766);p.add_argument('--open-browser',action='store_true')
    a=p.parse_args();root=Path(__file__).resolve().parents[2];output=(root/a.output).resolve()
    if a.command=='configure-market':
        if not a.market_db:p.error('--market-db is required')
        output.mkdir(parents=True,exist_ok=True)
        source=(root/a.market_db).resolve(strict=True)
        from .market_workspace import latest_snapshot
        market=latest_snapshot(source,now_utc().isoformat())
        from trade_system.file_lock import FileLock
        with FileLock(output/'update.guard'):
            write_pointer(output,'workspace-config.json',{'market_database':str(source),'read_only':True})
            publish_desk(output,market=market)
        result={'configured':True,'snapshot_id':market['snapshot_id'],'production_cutover':False}
    elif a.command=='market-update':
        from .daily_workspace import update_market
        result=update_market(output)
    elif a.command=='observe':result=observe(output,capture_quotes=a.capture_quotes,quote_receipts=a.quote_receipts)
    elif a.command=='present':
        result=present(output,market_review=root/a.market_review if a.market_review else None)
    elif a.command=='render':
        if not a.destination:p.error('--destination is required')
        result=render_saved(output,root/a.destination,market_review=root/a.market_review if a.market_review else None)
    elif a.command=='utility':
        if not a.destination:p.error('--destination is required for independent utility evidence')
        from .utility_verification import verify
        result=verify(output,root/a.destination)
    elif a.command=='preflight':
        from .research_recent import capacity_preflight
        result=capacity_preflight(read_json(root/a.config)[0],root)
    elif a.command=='price-study':
        if not a.donor: p.error('--donor is required for the frozen supplemental price study')
        from .price_study import run
        result=run(root,output,root/a.donor)
    elif a.command=='build': result=build(root/a.config,root,output)
    elif a.command=='update': result=update(root,output,receipts=a.receipts,replay_build=a.replay_build,force_refresh=a.force_refresh)
    elif a.command=='stop':
        from .research_product_server import stop
        result=stop(output,a.port)
    elif a.command=='serve':
        from .research_product_server import serve
        return serve(root,output,a.port,open_browser=a.open_browser)
    else:
        _,model,result=read_build(output);prediction=read_prediction(output)
        result={'model_id':model['model_id'],'folds':len(result['folds']),'prediction_date':prediction['date'] if prediction else None,
            'predictions':prediction['predictions'] if prediction else 0,'execution_ready':False}
        result['active_train_end']=model['train_end']
        if (output/'research-candidate.json').exists():
            candidate_run,candidate,_=read_build(output,pointer_name='research-candidate.json',historical=True)
            result['candidate_train_end']=candidate['train_end']
            result['candidate_readiness']=read_json(candidate_run/'publication-readiness.json')[0]
            from . import research_recent
            meta=dataset_metadata(candidate_run)
            result['candidate_formulas_current']=meta.get('recent_source_sha256')==file_hash(research_recent.__file__)
    print(canonical(result))


if __name__=='__main__':
    main()
