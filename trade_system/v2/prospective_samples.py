"""Freeze actually received future observations; calendar elapsed != qualified samples.

This intake retains incomplete cases, not forecasts or training permission.
Native raw prices cannot silently satisfy the protocol's adjusted T+2 label.
"""
from pathlib import Path

from . import daily_session, native_enrichment, prospective_registry
from .domain import identity, now_utc, utc
from .gap_evidence import read_json,write_json


def freeze(registration, report_folder, enrichment_folder, output, *, clock=now_utc):
    reg=prospective_registry.inspect(registration,clock=clock)
    plan=read_json(Path(registration)/'protocol.json')[0]
    report=daily_session.verify(report_folder)
    enrichment=native_enrichment.verify(enrichment_folder)
    now=utc(clock()); day=daily_session.local_day(now)
    if reg['calendar_window']!='open' or not plan['start_date']<=day<=plan['end_date']:
        raise ValueError('prospective collection window is not open')
    if report['trade_date']!=day or enrichment['trade_date']!=day or enrichment['parent_report_id']!=report['report_id']:
        raise ValueError('actual current session and exact cohort enrichment required; no retrospective backfill')
    if utc(report['generated_at'])>now or any(utc(r['received_at'])>now for r in enrichment['observations'].values()):
        raise ValueError('input not yet received')
    real=report['origin']=='hithink_native' and enrichment['origin']=='hithink_native'
    rows=[]
    for candidate in report['candidates']:
        code=candidate['instrument']
        rows.append({'instrument':code,'candidate_id':candidate['candidate_id'],
            'price_observation':enrichment['observations'].get(code),
            'topic_context':[t for t,v in enrichment['topics'].items() if code in v['cohort_members']],
            'feature_groups':{group:{feature:None for feature in features} for group,features in plan['variants'].items()},
            'blockers':['registered_factor_values_and_receipts_missing','limit_pool_not_full_PIT_universe',
                        'adjustment_basis_not_qualified','source_groups_not_jointly_qualified'],
            'eligible_for_fit':False})
    result={'scope':'prospective_observation_intake_not_forecast_or_training_permission',
        'registration_id':reg['registration_id'],'report_id':report['report_id'],
        'enrichment_id':enrichment['enrichment_id'],'trade_date':day,'received_at':now.isoformat(),
        'origin':'hithink_native' if real else 'synthetic_fixture','rows':rows,
        'qualified_paired_sessions':0,'fits_performed':0,'execution_ready':False}
    result['sample_id']=identity(result)
    output=Path(output).resolve()
    for source in (registration,report_folder,enrichment_folder):
        if output==Path(source).resolve() or Path(source).resolve() in output.parents:
            raise ValueError('intake cannot mutate sealed inputs')
    output.mkdir(parents=True,exist_ok=False)
    write_json(output/'sample.json',result)
    daily_session.seal(output)
    return result


def read_sample(folder):
    from .domain import file_hash
    folder=Path(folder).resolve();paths=list(folder.iterdir())
    if {p.name for p in paths}!={'sample.json','completed.json'} or any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError('immutable flat sample required')
    members={'sample.json':file_hash(folder/'sample.json')}
    if read_json(folder/'completed.json')[0]!={'members':members,'manifest_id':identity(members)}:
        raise ValueError('sample bytes changed')
    sample=read_json(folder/'sample.json')[0]
    if sample['sample_id']!=identity({k:v for k,v in sample.items() if k!='sample_id'}):
        raise ValueError('sample fingerprint changed')
    return sample


def maturity(sample_folder, following_report, t1_enrichment, t2_enrichment, *, clock=now_utc):
    sample=read_sample(sample_folder)
    following=daily_session.verify(following_report)
    future=[d for d in following['calendar'] if d>sample['trade_date']]
    now=utc(clock())
    if utc(following['generated_at'])>now:
        raise ValueError('following calendar receipt is in the future')
    result={'sample_id':sample['sample_id'],'asof':now.isoformat(),'scope':'future_sample_maturity_not_qualification',
            'qualified_paired_sessions':0,'fits_performed':0,'execution_ready':False}
    if len(future)<2 or now<utc(future[1]+'T16:00:00+08:00'):
        return {**result,'status':'awaiting_actual_T2_close_and_receipts','rows':[]}
    one=native_enrichment.verify(t1_enrichment);two=native_enrichment.verify(t2_enrichment)
    if (one['trade_date'],two['trade_date'])!=tuple(future[:2]) or any(
        e['parent_report_id']!=sample['report_id'] for e in (one,two)):
        raise ValueError('exact T1/T2 cohort receipts required; never skip missing sessions')
    rows=[]
    for row in sample['rows']:
        a=one['observations'].get(row['instrument']);b=two['observations'].get(row['instrument'])
        if any(utc(v['received_at'])>now for v in (a,b) if v):
            raise ValueError('future label evidence receipt')
        rows.append({'instrument':row['instrument'],'label_next_ret':None,
            'label_status':'adjusted_label_not_proven_from_raw_bars' if a and b else 'exact_session_price_missing',
            'raw_price_receipts':[v['response_sha256'] for v in (a,b) if v],
            'feature_blockers':row['blockers'],'eligible_for_fit':False})
    return {**result,'status':'receipts_observed_qualification_incomplete','rows':rows}


def queue_status(registration, sample_folders=(), *, clock=now_utc):
    """Read-only intake inventory. This schema cannot authorize any fitting."""
    reg=prospective_registry.inspect(registration,clock=clock)
    plan=read_json(Path(registration)/'protocol.json')[0]
    if len(sample_folders)>366:raise ValueError('bounded one-year intake inventory required')
    days=set();rows=[];missing={g:{f:0 for f in features} for g,features in plan['variants'].items()}
    for folder in sample_folders:
        sample=read_sample(folder)
        if (sample['registration_id']!=reg['registration_id'] or sample['trade_date'] in days
            or not plan['start_date']<=sample['trade_date']<=plan['end_date']
            or utc(sample['received_at'])>utc(clock())):
            raise ValueError('unique in-window samples bound to the same registration required')
        if (sample['qualified_paired_sessions']!=0 or sample['fits_performed']!=0 or sample['execution_ready'] is not False
            or len(sample['rows'])>20):
            raise ValueError('observation intake cannot self-certify qualification')
        days.add(sample['trade_date']);codes=set()
        for row in sample['rows']:
            if row['instrument'] in codes or row['eligible_for_fit'] is not False or not row['blockers']:
                raise ValueError('unique unqualified intake rows required')
            codes.add(row['instrument'])
            expected={g:{f:None for f in features} for g,features in plan['variants'].items()}
            if row['feature_groups']!=expected:
                raise ValueError('intake schema stores no qualified factors; use a separately verified factor product')
            for group,features in missing.items():
                for feature in features:missing[group][feature]+=1
        rows.append({'sample_id':sample['sample_id'],'trade_date':sample['trade_date'],
                     'origin':sample['origin'],'candidate_rows':len(sample['rows'])})
    return {'scope':'prospective_intake_inventory_not_fit_permission','registration_id':reg['registration_id'],
        'calendar_window':reg['calendar_window'],'observed_sessions':len(days),'samples':sorted(rows,key=lambda r:r['trade_date']),
        'missing_feature_observations':missing,'qualified_paired_sessions':0,'fits_performed':0,
        'minimum_paired_sessions':plan['minimum_paired_sessions'],
        'required_next_products':['point_in_time_universe','jointly_qualified_factor_receipts','mature_adjusted_labels'],
        'research_ready':False,'execution_ready':False}


def main():
    import argparse
    import json
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    f=sub.add_parser('freeze')
    for name in ('registration','report','enrichment','output'):f.add_argument('--'+name,required=True)
    m=sub.add_parser('maturity')
    for name in ('sample','following','t1','t2','output'):m.add_argument('--'+name,required=True)
    status=sub.add_parser('status')
    status.add_argument('--registration',required=True);status.add_argument('--samples',nargs='*',default=[])
    status.add_argument('--output',required=True)
    a=p.parse_args()
    if a.command=='freeze':result=freeze(a.registration,a.report,a.enrichment,a.output)
    elif a.command=='status':
        result=queue_status(a.registration,a.samples)
        output=Path(a.output).resolve()
        if any(Path(p).resolve()==output or Path(p).resolve() in output.parents for p in [a.registration,*a.samples]):
            raise ValueError('inventory output must not mutate immutable inputs')
        output.parent.mkdir(parents=True,exist_ok=True)
        write_json(output,result)
    else:
        result=maturity(a.sample,a.following,a.t1,a.t2)
        write_json(Path(a.output),result)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'}))


if __name__=='__main__':main()
