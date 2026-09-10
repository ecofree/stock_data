"""Seal a future research protocol; registration is not data/model qualification."""
from datetime import date
import json
from pathlib import Path

from .domain import canonical,file_hash,identity,now_utc,utc
from .daily_session import CST

GROUPS=('price_baseline','price_plus_moneyflow','price_plus_flow','price_plus_funds')
BLOCKERS=['point_in_time_universe_and_delisted_coverage_not_accepted',
          'feature_definition_unit_and_receipt_evidence_not_accepted',
          'future_paired_cohort_not_collected','future_labels_not_mature',
          'independent_qlib_comparison_not_run','human_research_acceptance_missing']


def register(plan_path,output,*,clock=now_utc):
    source=Path(plan_path).resolve(strict=True)
    output=Path(output).resolve()
    if source.stat().st_size>64000 or output.exists():
        raise ValueError('bounded protocol and new registration directory required')
    raw=source.read_bytes();plan=json.loads(raw)
    received=utc(clock())
    required={'schema','scope','start_date','end_date','minimum_paired_sessions','max_fits',
              'historical_design_sha256','variants','label_definition','selection_policy',
              'round_trip_cost_bps','seed','num_boost_round','num_threads','acceptance'}
    if set(plan)!=required or plan['schema']!=1 or plan['scope']!='prospective_research_only':
        raise ValueError('exact prospective protocol schema required')
    start=date.fromisoformat(plan['start_date']);end=date.fromisoformat(plan['end_date'])
    if start<=received.astimezone(CST).date() or end<start or (end-start).days>366:
        raise ValueError('registration must precede the first future session; bounded one-year window')
    if type(plan['max_fits']) is not int or not 4<=plan['max_fits']<=20:
        raise ValueError('explicit 4..20 fit budget required')
    if type(plan['minimum_paired_sessions']) is not int or not 20<=plan['minimum_paired_sessions']<=120:
        raise ValueError('minimum paired session count must be 20..120')
    variants=plan['variants']
    if set(variants)!=set(GROUPS):
        raise ValueError('four predeclared source-ablation groups required')
    for group,features in variants.items():
        if not isinstance(features,list) or not 1<=len(features)<=40 or len(set(features))!=len(features):
            raise ValueError('bounded unique feature lists required')
        if any(not isinstance(f,str) or not f.isidentifier() for f in features):
            raise ValueError('explicit feature identifiers required')
        if not set(variants['price_baseline']).issubset(features):
            raise ValueError('every comparison must retain the same price baseline')
    if plan['round_trip_cost_bps']!=[0,10,30,50] or plan['num_threads']!=2 or not 1<=plan['num_boost_round']<=100:
        raise ValueError('fixed cost scenarios and bounded training budget required')
    if plan['acceptance']!={'automatic_promotion':False,'negative_results_retained':True,
                           'paired_common_cohort':True,'train_only_preprocessing':True,
                           'actual_receipt_time_required':True,'untouched_holdout_required':True}:
        raise ValueError('research safeguards cannot be weakened')
    for field in ('label_definition','selection_policy'):
        if not isinstance(plan[field],str) or not 20<=len(plan[field])<=4000:
            raise ValueError('explicit target and selection/tradability policy required')
    digest=plan['historical_design_sha256']
    if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('historical design fingerprint required')
    body={'scope':'local_prospective_registration_not_independent_timestamp_authority',
          'received_at':received.isoformat(),'protocol_sha256':identity(plan),'plan_file_sha256':file_hash(source),
          'source_code_sha256':file_hash(Path(__file__)),'blockers':BLOCKERS,
          'research_ready':False,'execution_ready':False,'fits_performed':0}
    receipt={**body,'registration_id':identity(body)}
    output.mkdir(parents=True,exist_ok=False)
    (output/'protocol.json').write_bytes(raw)
    (output/'registration.json').write_text(canonical(receipt),encoding='utf-8')
    return receipt


def inspect(folder,*,clock=now_utc):
    folder=Path(folder).resolve(strict=True)
    paths=list(folder.iterdir())
    if {p.name for p in paths}!={'protocol.json','registration.json'} or any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError('immutable flat registration package required')
    receipt=json.loads((folder/'registration.json').read_text(encoding='utf-8'))
    plan=json.loads((folder/'protocol.json').read_text(encoding='utf-8'))
    if (identity({k:v for k,v in receipt.items() if k!='registration_id'})!=receipt['registration_id']
        or file_hash(folder/'protocol.json')!=receipt['plan_file_sha256'] or identity(plan)!=receipt['protocol_sha256']):
        raise ValueError('registration was changed')
    today=utc(clock()).astimezone(CST).date()
    if utc(receipt['received_at']).astimezone(CST).date()>=date.fromisoformat(plan['start_date']):
        raise ValueError('registration is not prospective')
    return {**receipt,'calendar_window':'not_started' if today<date.fromisoformat(plan['start_date'])
            else 'closed' if today>date.fromisoformat(plan['end_date']) else 'open',
            'note':'Elapsed time never qualifies data or authorizes fitting; use separate evidence.'}


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['register','status'])
    parser.add_argument('--source',required=True);parser.add_argument('--output')
    args=parser.parse_args()
    try:
        if args.operation=='register' and not args.output:
            raise ValueError('new output directory required')
        result=register(args.source,args.output) if args.operation=='register' else inspect(args.source)
        print(json.dumps(result,ensure_ascii=True));return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'error_type':type(exc).__name__,'execution_ready':False}));return 1


if __name__=='__main__':
    raise SystemExit(main())
