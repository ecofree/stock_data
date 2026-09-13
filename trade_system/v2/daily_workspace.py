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


def update_market(output):
    """Explicit local-data update, using the same owner as CLI/research/quotes."""
    from trade_system.file_lock import FileLock
    from .market_workspace import latest_snapshot
    from .research_product import publish_desk
    output=Path(output)
    with FileLock(output/'update.guard'):
        config=read_json(output/'workspace-config.json')[0]
        if config.get('read_only') is not True:raise ValueError('read-only market source required')
        market=latest_snapshot(config['market_database'],now_utc().isoformat())
        publish_desk(output,market=market)
        return {'date':market['trade_date'],'snapshot_id':market['snapshot_id'],
                'provider_requests':0,'fits':0,'execution_ready':False}


def seal_evidence(output, snapshot):
    """A small immutable market reference for a judgement, not a copied whole page."""
    from .research_journal import durable_event
    value=dict(snapshot)
    evidence_id=identity(value)
    durable_event(Path(output)/'notes/evidence', evidence_id, value)
    return evidence_id
