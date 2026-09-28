"""Daily use-case projection. Market and human attention do not require research."""
import json
from pathlib import Path

from .domain import identity, now_utc
from .gap_evidence import read_json
from .publisher import read_current


def empty_projection():
    return {'scope':'local_daily_product_no_execution', 'execution_ready':False,
        'research':{'aggregate':{},'paired_comparisons':{},'folds':[]}, 'model':{},
        'dataset':{'summary':{'universe_count':0},'dataset_config':{'start':'—','end':'—'},'rows':0},
        'prediction':None,'notes':[],'reviews':[], 'research_status':'not_configured'}


def action_queue(data):
    """Risk first, then plan changes. Viewing an item never resolves it."""
    plans=data.get('plans') or {};account=plans.get('account') or {}
    rows=[]
    if account.get('status') != 'account_snapshot_available_not_execution' or account.get('blockers'):
        rows.append({'priority':0,'kind':'account_verification','instrument':None,
            'reason':account.get('status','account_unknown'),'allowed_action':'核对账户资料；收益与仓位保持未知'})
    for item in account.get('open_orders',[]):
        rows.append({'priority':0,'kind':'unresolved_order','instrument':item.get('instrument'),
            'reason':'未完成委托需人工对账','allowed_action':'核对券商回执；本页不发送委托'})
    for item in account.get('positions',[]):
        if item.get('quantity',0)>0:
            rows.append({'priority':1,'kind':'existing_position','instrument':item['instrument'],
                'reason':'已有持仓需核对原计划和数据时效','allowed_action':'查看持仓及原判断'})
    for plan in plans.get('rows',[]):
        if not plan.get('is_latest'):continue
        state=plan.get('evaluation',{}).get('state','unknown')
        rows.append({'priority':1 if state in ('expired','invalidated','triggered','account_conflict') else 2,
            'kind':'observation_plan','instrument':plan['instrument'],'reason':state,
            'plan_id':plan['plan_id'],'valid_until':plan.get('valid_until'),
            'allowed_action':'核对条件并追加判断；查看不解除风险'})
    return sorted(rows,key=lambda r:(r['priority'],r.get('instrument') or '',r.get('plan_id','')))


def period_history(output, market):
    """Use only attested historical publications, never staged runs or new I/O."""
    from copy import deepcopy
    from .publisher import read_bundle
    from trade_system.review_metrics import theme_evolution
    result=deepcopy(market)
    if not result.get('periods'):return result
    root=Path(output)/'publication'
    history={};errors=[]
    if (root/'current.json').exists():
        manifest,files=read_current(root)
        pointers=dict(manifest.get('published_days',{}))
        previous=json.loads(files['desk.json']).get('market') or {}
        if previous.get('trade_date'):
            history[previous['trade_date']]=previous
        if len(pointers)>94:raise ValueError('period publication history exceeds quarter budget')
        earliest=min(p['start'] for p in result.get('periods',{}).values())
        for day,pointer in pointers.items():
            if not earliest<=day<result['trade_date']:continue
            try:
                _,body=read_bundle(root,pointer)
                saved=json.loads(body['desk.json'])['market']
                if saved['trade_date']!=day:raise ValueError('publication date mismatch')
                history[day]=saved
            except (OSError,ValueError,KeyError):
                errors.append(day)
    history[result['trade_date']]=result
    for period,value in result.get('periods',{}).items():
        sessions=[]
        for day in value['expected_sessions']:
            saved=history.get(day,{})
            observed=next((r for r in saved.get('periods',{}).get('week',{}).get('themes',[])
                           if r['date']==day),None)
            if observed:
                sessions.append(dict(observed,snapshot_id=saved['snapshot_id']))
        value['theme_evolution']=theme_evolution(sessions,value['expected_sessions'])
        value['theme_evolution']['publication_errors']=[d for d in errors if d in value['expected_sessions']]
        value['theme_evolution']['source']='attested_daily_publications_current_local_projection'
    result.pop('snapshot_id',None)
    return dict(result,snapshot_id=identity(result))


def projection(output, *, market=None, research_loader=None):
    from .research_product import journal_projection
    output=Path(output); data=empty_projection()
    if research_loader and (output/'research-current.json').exists():
        try:
            data.update(research_loader(output));data['research_status']='frozen_evidence'
        except (ImportError, OSError, ValueError, KeyError) as exc:
            data['research_status']='unavailable'
            data['research_error']=type(exc).__name__+': '+str(exc)[:200]
    if market is None and (output/'publication/current.json').exists():
        _, files=read_current(output/'publication')
        market=json.loads(files['desk.json']).get('market')
    data['market']=market
    forecast=data.get('prediction')
    data['prediction_matches_market']=(market['trade_date']==forecast['date']) if market and forecast else None
    if forecast and market and not data['prediction_matches_market']:
        data['research_status']='historical_prediction_not_current_candidates'
    return journal_projection(output,data)


def update_market(output, *, expected_date=None):
    """Explicit local-data update, using the same owner as CLI/research/quotes."""
    from trade_system.file_lock import FileLock
    from .market_workspace import latest_snapshot
    from .research_product import publish_desk
    output=Path(output)
    with FileLock(output/'update.guard'):
        config=read_json(output/'workspace-config.json')[0]
        if config.get('read_only') is not True:raise ValueError('read-only market source required')
        market=latest_snapshot(config['market_database'],now_utc().isoformat())
        if expected_date and market['trade_date'] != expected_date:
            if market.get('session_state') == 'closed' and market.get('calendar_checked_date') == expected_date:
                return {'status':'market_closed','date':market['trade_date'],
                        'expected_date':expected_date,'provider_requests':0,'fits':0,'execution_ready':False}
            raise ValueError(f"expected market session {expected_date}, received {market['trade_date']}; keep last publication")
        market=period_history(output,market)
        publish_desk(output,market=market)
        return {'status':'market_published','date':market['trade_date'],'snapshot_id':market['snapshot_id'],
                'provider_requests':0,'fits':0,'execution_ready':False}


def seal_evidence(output, snapshot):
    """A small immutable market reference for a judgement, not a copied whole page."""
    from .research_journal import durable_event
    value=dict(snapshot)
    evidence_id=identity(value)
    durable_event(Path(output)/'notes/evidence', evidence_id, value)
    return evidence_id
