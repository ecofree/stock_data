"""Local research delivery: existing dataset, QLib runner, observations and journal."""
import argparse
from pathlib import Path
import uuid

from .domain import canonical, file_hash, identity, now_utc
from .gap_evidence import read_json, write_json
from .publisher import publish, read_current


def write_pointer(root, name, value):
    temp=Path(root)/('.'+name+'-'+uuid.uuid4().hex)
    write_json(temp,value); temp.replace(Path(root)/name)


def build(config_path, root, output):
    """Retired active trainer; standalone historical experiment readers remain."""
    raise ValueError('RESEARCH_RETIRED: active model training, prediction refresh and automatic promotion are retired; retained historical evidence is read-only')

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
    """Refuse all former CLI/HTTP refresh paths before locks, requests or writes."""
    raise ValueError('RESEARCH_RETIRED: active model training, prediction refresh and automatic promotion are retired; retained historical evidence is read-only')

def configured_market(output, prediction):
    path=Path(output)/'workspace-config.json'
    if not path.exists():return None
    from .market_workspace import latest_snapshot
    config=read_json(path)[0]
    return latest_snapshot(config['market_database'],now_utc().isoformat(),
                    [r['instrument'] for r in prediction['rows']],research_workspace=output)


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
    """Private spelling cannot restore the retired active update owner."""
    raise ValueError('RESEARCH_RETIRED: active model training, prediction refresh and automatic promotion are retired; retained historical evidence is read-only')

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
    if name in ('research-current.json', 'research-candidate.json', 'prediction-current.json'):
        raise ValueError('RESEARCH_RETIRED: active model training, prediction refresh and automatic promotion are retired; retained historical evidence is read-only')
    path=Path(output)/name
    with FileLock(Path(output)/'publication.guard'):
        previous=read_json(path)[0] if path.exists() else None
        write_pointer(output,name,value)
        try:return _publish_desk(output) if market is None else _publish_desk(output,market=market)
        except Exception:
            if previous is not None:write_pointer(output,name,previous)
            else:path.unlink()
            raise


def _retire_active_forecasts(data):
    """Retain evidence without exposing it as current forecasts or promotion."""
    if data.get('prediction') is not None:
        data['retained_prediction_evidence'] = data['prediction']
    if data.get('model'):
        data['retained_model_evidence'] = data['model']
    data.update(prediction=None, model={}, candidate_model=None, candidate_readiness=None,
                prediction_matches_market=None, execution_ready=False,
                research_retirement={'status':'retired', 'active_predictions':False,
                                     'automatic_promotion':False,
                                     'retained_history':'read_only_original_artifacts'})
    data.pop('report_id', None)
    data['report_id'] = identity(data)
    return data


def _publish_desk(output,*,market=None):
    from .daily_workspace import projection, period_history
    from .research_product_view import render
    if market is not None:market=period_history(output,market)
    data=_retire_active_forecasts(projection(output,market=market,research_loader=_research_projection))
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
    from .journal_index import recent
    data=deepcopy(data);data.pop('report_id',None)
    note_paths,note_total=recent(output,'note')
    notes=sorted([research_journal.read_note(output,p.stem) for p in note_paths],key=lambda n:n['received_at'])
    data['notes']=research_journal.annotate(notes)
    data['attention_followups']=research_journal.attention_followups(output,data['notes'],data.get('market'))
    review_paths,_=recent(output,'review')
    data['human_reviews']=[research_journal.read_review(output,p.stem) for p in reversed(review_paths)]
    data['note_scope']={'shown':len(notes),'total':note_total,'limit':100}
    if data.get('market'):
        from datetime import date,timedelta
        from trade_system.review_metrics import period_bounds, judgement_period_summary
        from .journal_index import between
        from .accounts import configured_performance
        day=data['market']['trade_date'];start=min(period_bounds(day,p)[0] for p in ('week','quarter'))
        stop=(date.fromisoformat(day)+timedelta(days=1)).isoformat()
        data['period_notes']=between(output,'note',start+'T00:00:00+08:00',stop+'T00:00:00+08:00')
        data['period_note_scope']={'start':start,'through':day,'complete':True,'count':len(data['period_notes'])}
        period_reviews=between(output,'review',start+'T00:00:00+08:00',stop+'T00:00:00+08:00')
        data['method_periods']={p:judgement_period_summary(day,p,data['period_notes'],period_reviews)
                                for p in ('day','week','month','quarter')}
        import duckdb
        try:data['account_periods']=configured_performance(output,day)
        except (OSError,ValueError,KeyError,duckdb.Error):
            data['account_periods']={p:{'start':period_bounds(day,p)[0],'end':period_bounds(day,p)[1],'through':day,
                'status':'insufficient_account_evidence','missing':['account_source_or_ledger_invalid']} for p in ('month','quarter')}
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
            manifest,files=read_current(observation)
            import hashlib
            data['observation_evidence']={'run_id':manifest['run_id'],'generation':manifest['generation'],
                'manifest_sha256':hashlib.sha256(canonical(manifest).encode()).hexdigest()}
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
    from .daily_workspace import action_queue
    data['action_queue']=action_queue(data)
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
        data=_retire_active_forecasts(saved_projection(output))
        codes={r['instrument'] for r in (data.get('prediction') or {}).get('rows',[])}
        from .journal_index import effective
        attention={n['instrument'] for n in effective(output,'note')}
        codes.update(attention)
        from .domain import utc
        clock=now_utc();warnings=[];sampling=None
        plans=data.get('plans') or {};account=plans.get('account') or {}
        risk_entries=[*(p for p in account.get('positions',[]) if p.get('quantity',0)>0),
                      *account.get('open_orders',[]),*account.get('internal_reservations',[])]
        if account.get('status') in {'account_unknown','account_source_unavailable','risk_unavailable'} and (output/'publication/current.json').exists():
            import json
            try:
                _,sealed_files=read_current(output/'publication')
                last_account=(json.loads(sealed_files['desk.json']).get('plans') or {}).get('account') or {}
                risk_entries.extend(p for p in last_account.get('positions',[]) if p.get('quantity',0)>0)
                risk_entries.extend(last_account.get('open_orders',[]))
                risk_entries.extend(last_account.get('internal_reservations',[]))
                if risk_entries:warnings.append('account_unknown_retaining_last_published_risk_scope')
            except (OSError,ValueError,KeyError,TypeError):
                warnings.append('last_published_account_scope_unavailable')
        priority=set()
        for entry in risk_entries:
            instrument=entry.get('instrument','')
            code=instrument.split('.')[-1] if instrument.startswith(('SH.','SZ.','BJ.')) else instrument.split('.')[0]
            if len(code)==6 and code.isdigit():priority.add(code)
        for plan in effective(output,'plan'):
            if utc(plan['valid_until'])>utc(clock):priority.add(plan['instrument'])
        codes.update(priority)
        previous_scope={}
        if (output/'observation-publication/current.json').exists():
            try:
                import json
                _,previous_files=read_current(output/'observation-publication')
                previous=json.loads(previous_files['observation.json'])
                previous_scope={r['instrument']:r for r in previous.get('live_scope',[])}
                if account.get('status') in {'account_unknown','account_source_unavailable','risk_unavailable'}:
                    priority.update(c for c,r in previous_scope.items() if r.get('risk_related'))
                    codes.update(priority)
                    warnings.append('account_unknown_retaining_last_known_risk_scope')
            except (OSError,ValueError,KeyError,TypeError):
                warnings.append('previous_observation_unavailable_risk_scope_unknown')
        if (output/'sampling-current.json').exists():
            try:
                pointer=read_json(output/'sampling-current.json')[0]
                from .observation_workspace import local_clock
                sampling=observation_capture.read_sampling(pointer['folder'],local_clock(clock).date().isoformat())
                if sampling['sampling_id']!=pointer['sampling_id']:raise ValueError('sampling pointer changed')
                codes.update(sampling['codes'])
            except (OSError,ValueError,KeyError,TypeError):
                sampling=None;warnings.append('no_verified_pre_session_scope_for_today')
        live_scope=[{'instrument':c,'included_at':previous_scope.get(c,{}).get('included_at',clock.isoformat()),
                     'risk_related':c in priority,
                     'roles':(['risk_or_active_plan'] if c in priority else [])+
                         (['human_attention'] if c in attention else [])+
                         (['pre_session_subject'] if sampling and c in sampling['codes'] else [])+
                         (['research_forecast'] if any(r['instrument']==c for r in (data.get('prediction') or {}).get('rows',[])) else [])}
                    for c in sorted(codes)]
        # Oldest attempted subjects first inside each priority tier. Failed/empty
        # responses also advance the cursor, so unavailable codes cannot starve others.
        def turn(code):
            return (previous_scope.get(code,{}).get('last_attempted_at',''),code)
        selected=set((sorted(priority,key=turn)+sorted(codes-priority,key=turn))[:200])
        deferred=sorted(codes-selected)
        for subject in live_scope:
            subject['last_attempted_at']=(clock.isoformat() if subject['instrument'] in selected
                else previous_scope.get(subject['instrument'],{}).get('last_attempted_at',''))
        rows=load_rows(config['market_database'],selected,clock.isoformat())
        value=project_rows(rows,selected,clock.isoformat());acquired=None;requests=0;reused=False
        cached=None;cached_receipt=None;recent=[];recent_codes=set()
        if (output/'quote-capture-current.json').exists():
            try:
                cached=read_json(output/'quote-capture-current.json')[0]
                cached_receipt=observation_capture.replay(cached['folder'])
                if cached_receipt['manifest_id']!=cached['manifest_id'] or cached_receipt['origin']!='tencent_https':
                    raise ValueError('quote capture cache identity differs')
            except (OSError,ValueError,KeyError,TypeError):
                cached=None;cached_receipt=None;warnings.append('quote_cache_unusable')
        if cached_receipt is not None:
            entries=cached.get('recent',[])
            if not isinstance(entries,list) or len(entries)>199:
                entries=[];warnings.append('quote_cache_index_unusable')
            entries=[*entries,{k:cached[k] for k in ('folder','captured_at','manifest_id')}]
            for entry in entries:
                try:
                    if not 0<=(clock-utc(entry['captured_at'])).total_seconds()<60:continue
                    receipt=cached_receipt if entry['folder']==cached['folder'] else observation_capture.replay(entry['folder'])
                    if receipt['manifest_id']!=entry['manifest_id'] or receipt['origin']!='tencent_https':raise ValueError('cache identity')
                    recent.append(entry);recent_codes.update(receipt['codes'])
                    rows.extend(r for r in receipt['rows'] if r['asset_code'] in selected and r not in rows)
                except (ValueError,TypeError,KeyError,OSError):
                    warnings.append('quote_cache_part_unusable')
            value=project_rows(rows,selected,clock.isoformat())
            reused=bool(recent_codes & selected)
        missing=[r['instrument'] for r in value['rows'] if r['state']=='no_qualified_current_quote']
        if quote_receipts:
            acquired=observation_capture.replay(quote_receipts)
            if acquired['origin']!='tencent_https' or not set(acquired['codes'])<=codes:
                raise ValueError('real same-universe quote replay required')
        elif capture_quotes and missing:
            pending=[c for c in missing if c not in recent_codes]
            reused=reused or len(pending)<len(missing)
            missing=pending
            if not missing:acquired=cached_receipt
            if acquired is None and missing:
                acquired=observation_capture.capture(output/'quote-captures'/uuid.uuid4().hex,missing)
                requests=acquired['requests']
                write_pointer(output,'quote-capture-current.json',{'captured_at':now_utc().isoformat(),
                    'codes':missing,'folder':acquired['folder'],'manifest_id':acquired['manifest_id'],
                    'recent':recent[-199:]})
        if acquired is None and not quote_receipts and cached_receipt is not None:
            acquired=cached_receipt
            reused=True
        if acquired:
            retained=[r for r in acquired['rows'] if r['asset_code'] in selected and r not in rows]
            value=project_rows([*rows,*retained],selected,now_utc().isoformat())
            value.pop('snapshot_id')
            value['capture']={k:v for k,v in acquired.items() if k not in ('rows','codes')}
            value['capture'].update(provider_requests_this_run=requests,reused=reused,
                                    received_rows=len(acquired['rows']),reused_receipts=recent)
            value['snapshot_id']=identity(value)
        value.pop('snapshot_id')
        value['rows'].extend({'instrument':c,'state':'capacity_blocked','price':None,'provider':None,
            'source_event_time':None,'valid_until':None,'rejected_reasons':['observation_budget_exceeded'],
            'retained_rows':0} for c in deferred)
        value['live_scope']=live_scope;value['warnings']=warnings
        value['account_status']=account.get('status','account_unknown')
        value['sampling']={'sampling_id':sampling['sampling_id'],'rows':sampling['rows'],
            'account_status':sampling['account_status']} if sampling else {'status':'no_pre_session_scope_current_observation_only'}
        value['snapshot_id']=identity(value)
        destination=output/'observation-publication'
        generation=read_current(destination)[0]['generation']+1 if (destination/'current.json').exists() else 1
        publish(destination,uuid.uuid4().hex,{'observation.json':canonical(value).encode()},generation=generation)
        return {'snapshot_id':value['snapshot_id'],'qualified':value['qualified'],'securities':len(value['rows']),
                'provider_requests':requests,'received_rows':len(acquired['rows']) if acquired else 0,
                'capture_reused':reused,'capture_failures':acquired['failures'] if acquired else 0,
                'fits':0,'execution_ready':False}


def _checked_market_review(path):
    market=read_json(path)[0]
    if market.get('snapshot_id')!=identity({k:v for k,v in market.items() if k!='snapshot_id'}):
        raise ValueError('market snapshot identity changed')
    if market.get('scope')!='read_only_market_review_not_execution' or market.get('execution_ready') is not False:
        raise ValueError('non-executable market snapshot required')
    return market


def saved_projection(output,*,market_review=None):
    """Read a sealed projection without network/model execution."""
    import json
    _,files=read_current(Path(output)/'publication')
    data=json.loads(files['desk.json'])
    if data['report_id']!=identity({k:v for k,v in data.items() if k!='report_id'}):
        raise ValueError('published projection identity changed')
    data=journal_projection(output,data)
    if market_review:
        market=_checked_market_review(market_review)
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
        data=_retire_active_forecasts(saved_projection(output))
        if market_review:
            data['market']=_checked_market_review(market_review)
            data=journal_projection(output,data)
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
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['build','update','market-update','observe','serve','stop','status','preflight','price-study','utility','render','present','configure-market','register-sampling','request-metrics','journal-index','journal-history'])
    p.add_argument('--kind',choices=['note','review','plan'],default='note');p.add_argument('--before')
    p.add_argument('--ensure-index',action='store_true',help='Rebuild event cache only when missing or invalidated; creates no human events')
    p.add_argument('--config',default='config/research_delivery.json');p.add_argument('--output',default='reports/research-delivery')
    p.add_argument('--receipts');p.add_argument('--replay-build',action='store_true',help='Replay only the frozen training receipts; does not certify the latest market date')
    p.add_argument('--destination',help='New isolated destination for a read-only render')
    p.add_argument('--expected-date',help='Scheduled market publication must match this ISO trading date')
    p.add_argument('--force-refresh',action='store_true',help='Explicitly recapture the bounded revision window instead of reusing the sealed session')
    p.add_argument('--market-review',help='Same-date immutable market review projection')
    p.add_argument('--market-db',help='Explicit read-only canonical database for daily workspace')
    p.add_argument('--donor',help='Sealed receipt batch supplying exactly one failed factor request')
    p.add_argument('--capture-quotes',action='store_true',help='Explicit bounded Tencent fallback observation, not execution')
    p.add_argument('--session',help='ISO prospective sampling session; registration must precede 09:15')
    p.add_argument('--controls',default='',help='Explicit predeclared comma-separated comparison securities')
    p.add_argument('--quote-receipts',help='Replay a sealed real quote receipt folder without network')
    p.add_argument('--port',type=int,default=8766);p.add_argument('--open-browser',action='store_true')
    a=p.parse_args();root=Path(__file__).resolve().parents[2];output=(root/a.output).resolve()
    if a.command=='journal-history':
        from .journal_index import history
        result=history(output,a.kind,a.before)
    elif a.command=='journal-index':
        from .journal_index import rebuild, ensure
        from trade_system.file_lock import FileLock
        with FileLock(output/'judgement.guard'):
            if a.ensure_index:
                ensure(output);result={'index_ready':True,'business_events_created':0}
            else:result=rebuild(output)
    elif a.command=='configure-market':
        if not a.market_db:p.error('--market-db is required')
        output.mkdir(parents=True,exist_ok=True)
        source=(root/a.market_db).resolve(strict=True)
        from .market_workspace import latest_snapshot
        market=latest_snapshot(source,now_utc().isoformat())
        from trade_system.file_lock import FileLock
        with FileLock(output/'update.guard'):
            config=read_json(output/'workspace-config.json')[0] if (output/'workspace-config.json').exists() else {}
            write_pointer(output,'workspace-config.json',dict(config,market_database=str(source),read_only=True))
            publish_desk(output,market=market)
        result={'configured':True,'snapshot_id':market['snapshot_id'],'production_cutover':False}
    elif a.command=='market-update':
        from .daily_workspace import update_market
        result=update_market(output,expected_date=a.expected_date)
    elif a.command=='request-metrics':
        if not a.receipts:p.error('--receipts must name an existing request log')
        from trade_system.http_transport import request_metrics
        result=request_metrics([root/a.receipts])
    elif a.command=='register-sampling':
        if not a.session:p.error('--session required')
        from .observation_capture import register_sampling
        result=register_sampling(output,a.session,[c for c in a.controls.split(',') if c])
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
        raise ValueError('RESEARCH_RETIRED: active model training, prediction refresh and automatic promotion are retired; retained historical evidence is read-only')
    elif a.command=='build': result=build(root/a.config,root,output)
    elif a.command=='update': result=update(root,output,receipts=a.receipts,replay_build=a.replay_build,force_refresh=a.force_refresh)
    elif a.command=='stop':
        from .research_product_server import stop
        result=stop(output,a.port)
    elif a.command=='serve':
        from .research_product_server import serve
        return serve(root,output,a.port,open_browser=a.open_browser)
    else:
        result={'status':'retired', 'active_predictions':False, 'automatic_promotion':False,
                'retained_history':'read_only_original_artifacts', 'provider_requests':0,
                'fits':0, 'execution_ready':False}
    print(canonical(result))


if __name__=='__main__':
    main()
