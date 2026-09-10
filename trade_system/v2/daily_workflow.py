"""Bounded post-close observation run. Pending is not a successful daily close."""
from pathlib import Path

from . import daily_session as daily
from .daily_session_view import render,render_followup
from .domain import canonical,identity,now_utc,utc
from .gap_evidence import write_json
from .publisher import publish


def run(output,*,clock=now_utc,client=None,parent=None,archive=None,publish_root=None,generation=None):
    output=Path(output).resolve()
    moment=utc(clock())
    day=daily.local_day(moment)
    # Never nest mutable output under an immutable source package or note archive.
    for source in (parent,archive,publish_root):
        if source is not None and (Path(source).resolve()==output or Path(source).resolve() in output.parents):
            raise ValueError('run output must be separate from source and publication directories')
    if (publish_root is None)!=(generation is None):
        raise ValueError('publication root and generation must be specified together')
    output.mkdir(parents=True,exist_ok=False)
    base={'scope':'daily_observation_workflow_not_validated_selection','trade_date':day,
          'started_at':moment.isoformat(),'execution_ready':False,'actual_operator_return':None}
    write_json(output/'started.json',base)
    if moment.astimezone(daily.CST).hour<15:
        pending={**base,'status':'awaiting_market_close','native_requests':0,
                 'daily_close_complete':False,'human_judgement':'not_created_by_system'}
        write_json(output/'pending.json',pending)
        return pending
    try:
        report=daily.capture(day,output/'observation',client=client,clock=clock)
        report=daily.verify(output/'observation')
        if report['status']!='current_native_observation':
            result={**base,'status':'source_check_required','gaps':report['gaps'],
                    'daily_close_complete':False,'report_id':report['report_id']}
            write_json(output/'pending.json',result)
            return result
        notes=daily.read_judgements(archive,report,asof=utc(clock()))
        queue={'report_id':report['report_id'],'scope':'human_judgement_required_not_auto_decision',
               'items':[{'candidate_id':c['candidate_id'],'instrument':c['instrument'],
                         'state':'received' if any(n['note']['candidate_id']==c['candidate_id'] for n in notes) else 'awaiting_human',
                         'support':c['support'],'provider_reason':c['provider_reason'],'risks':c['risks']}
                        for c in report['candidates']], 'execution_ready':False}
        write_json(output/'human_queue.json',queue)
        artifacts={'index.html':render(report).encode('utf-8'),'observation.json':canonical(report).encode('utf-8')}
        review=None
        if parent:
            previous=daily.verify(parent)
            review=daily.next_review(previous,report,daily.read_judgements(archive,previous,asof=utc(clock())),created=utc(clock()))
            write_json(output/'next_session_review.json',review)
            artifacts['review.html']=render_followup(review).encode('utf-8')
            artifacts['review.json']=canonical(review).encode('utf-8')
        result={**base,'status':'observation_ready_awaiting_human','daily_close_complete':True,
                'report_id':report['report_id'],'candidate_count':len(report['candidates']),
                'judgements_received':len(notes),'next_session_review_created':review is not None,
                'human_loop_complete':False,'publication':None}
        if publish_root is not None:
            result['publication']=publish(publish_root,identity([day,report['report_id']]),artifacts,generation=generation)
        write_json(output/'completed.json',result)
        return result
    except Exception as exc:
        write_json(output/'failed.json',{**base,'status':'failed','error_type':type(exc).__name__,
                                        'daily_close_complete':False})
        raise
