"""Current native observation, offline judgement import, exact next-session review."""
import argparse
import json
from pathlib import Path

from .daily_session import capture, import_judgement, verify, next_review, read_judgements, legacy_observations
from .daily_session_view import render, render_followup
from .domain import now_utc
from .gap_evidence import write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command',required=True)
    c = sub.add_parser('capture'); c.add_argument('--date',required=True); c.add_argument('--output',required=True)
    v = sub.add_parser('view'); v.add_argument('--report',required=True); v.add_argument('--output',required=True)
    n = sub.add_parser('judge'); n.add_argument('--report',required=True); n.add_argument('--note',required=True); n.add_argument('--archive',required=True)
    r = sub.add_parser('review'); r.add_argument('--parent',required=True); r.add_argument('--following'); r.add_argument('--archive'); r.add_argument('--legacy-db'); r.add_argument('--output',required=True)
    a = p.parse_args()
    try:
        if a.command == 'capture':
            r = capture(a.date,a.output)
            result = {k:r[k] for k in ('report_id','status','trade_date','pool_total','gaps')}
            result['candidates'] = len(r['candidates'])
        elif a.command == 'view':
            report = verify(a.report)
            output = Path(a.output).resolve()
            source = Path(a.report).resolve()
            if output == source or source in output.parents:
                raise ValueError('view must not mutate sealed source package')
            output.mkdir(parents=True,exist_ok=False)
            write_json(output/'source.json',{'report_id':report['report_id'],'source_package':str(source)})
            (output/'index.html').write_text(render(report),encoding='utf-8')
            result = {'output':str(output),'report_id':report['report_id'],'scope':'read_only_view_of_frozen_daily_package'}
        elif a.command == 'judge':
            path = Path(a.note)
            if path.stat().st_size > 32000:
                raise ValueError('judgement byte budget exceeded')
            result = import_judgement(path.read_bytes(),a.report,a.archive)
        else:
            parent = verify(a.parent)
            output = Path(a.output)
            if output.exists():
                raise FileExistsError('new review output directory required')
            created = now_utc()
            notes = read_judgements(a.archive,parent,asof=created)
            if not a.following:
                if a.legacy_db:
                    raise ValueError('no price query before next-session identity is established')
                result = {'scope':'pending_next_session_not_completed_review',
                    'parent_report_id':parent['report_id'],'trade_date':None,
                    'created_at':created.isoformat(),'status':'awaiting_next_native_session',
                    'cohort_size':len(parent['candidates']),'judgements_received':len(notes),
                    'rows':[{'instrument':c['instrument'],'name':c['name'],'observation_status':'pending',
                             'judgement_status':'not_yet_evaluated',
                             'judgements':[{'note':n['note'],'timing':'pending_next_session_calendar',
                                            'receipt_id':n['receipt_id'],'received_at':n['received_at']}
                                           for n in notes if n['note']['candidate_id']==c['candidate_id']],
                             'observation':None} for c in parent['candidates']],
                    'actual_operator_return':None,'execution_ready':False}
            else:
                following = verify(a.following)
                # Validate session identity before any optional legacy database access.
                result = next_review(parent,following,notes,created=created)
                if a.legacy_db and parent['candidates']:
                    bars = legacy_observations(a.legacy_db,following['trade_date'],
                                               [c['instrument'] for c in parent['candidates']])
                    result = next_review(parent,following,notes,created=now_utc(),observations=bars)
            output.mkdir(parents=True,exist_ok=False)
            write_json(output/'review.json',result)
            (output/'index.html').write_text(render_followup(result),encoding='utf-8')
            result = {'output':str(output),'scope':result['scope'],'cohort_size':result['cohort_size']}
        print(json.dumps({**result,'execution_ready':False},ensure_ascii=False))
        return 0
    except Exception as exc:
        # Persisted incomplete capture retains its own failure class, no secret-bearing text.
        print(json.dumps({'ok':False,'error_type':type(exc).__name__,'execution_ready':False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
