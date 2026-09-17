"""Bounded window-invariance investigation of frozen conflicts, never repair authority."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2 import probe_identity_sources as probe
from trade_system.v2.daily_session import CST,seal
from trade_system.v2.domain import canonical,file_hash,identity,now_utc,number,utc
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.research_receipts import sealed

SCOPE='frozen_price_conflict_window_probe_not_canonical_PIT_or_execution_authority'
DAYS=('2025-11-27','2025-12-01')


def parent(folder):
    folder=Path(folder).resolve(strict=True)
    if {p.name for p in folder.iterdir()}!={'result.json','completed.json'}:raise ValueError('exact parent members required')
    path=folder/'result.json'
    if path.is_symlink() or path.stat().st_size>64_000_000:raise ValueError('bounded parent analysis required')
    members={'result.json':file_hash(path)}
    if read_json(folder/'completed.json')[0]!={'members':members,'manifest_id':identity(members)}:raise ValueError('parent seal differs')
    # The legacy annual result is ~49 MB. This explicit import bound does not
    # weaken the general 4 MB receipt reader; new results are sharded by code.
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('duplicate parent JSON key')
            result[k]=v
        return result
    with path.open(encoding='utf-8') as stream:result=json.load(stream,object_pairs_hook=unique)
    if result.get('execution_ready') is not False or result.get('production_cutover') is not False or result.get('source_unchanged') is not True:raise ValueError('isolated parent required')
    cases={};keys=set()
    for row in result['records']:
        if row['status']!='quarantined':continue
        key=(row['stock_code'],row['date'])
        if row['record_id']!=identity({k:v for k,v in row.items() if k!='record_id'}):raise ValueError('parent record identity differs')
        if key in keys or key[1] not in DAYS or not re.fullmatch(r'\d{6}',key[0]):raise ValueError('bounded unique conflict key required')
        keys.add(key);variants=row['source_variants']
        if not variants:raise ValueError('original conflict variants required')
        evidence=variants[0]['evidence'];codes={e['request_code'] for e in evidence}
        if len(codes)!=1:raise ValueError('unambiguous conflict identity required')
        code=next(iter(codes))
        if not re.fullmatch(key[0]+r'\.(SZ|SH)',code):raise ValueError('exact conflict identity required')
        if not {'hithink_native','xiaodefa_relay'}<={e['provider'] for e in evidence}:raise ValueError('dual-source conflict required')
        for variant in variants:
            if variant['evidence']!=evidence or variant['original_sha256']!=identity(variant['original']):raise ValueError('parent variant binding differs')
            original=variant['original']
            if original['date']!=key[1] or original['stock_code']!=key[0] or original['ts_code'] not in (code,code[-2:]+'.'+key[0]):raise ValueError('parent original identity differs')
        if any(e['date']!=key[1] for e in evidence):raise ValueError('parent evidence date differs')
        cases.setdefault(code,[]).append({'date':key[1],'parent_record_id':row['record_id'],'evidence':evidence})
    if not 1<=len(cases)<=38 or not 1<=len(keys)<=69 or len(keys)!=result['quarantined_duplicate_keys']:raise ValueError('bounded frozen conflict inventory required')
    return {'analysis_sha256':members['result.json'],'database_sha256':result['database_sha256'],'cases':cases}


def plan(code):
    if not re.fullmatch(r'\d{6}\.(SZ|SH)',code):raise ValueError('explicit code required')
    requests=[]
    for role,start,end in [('wide','2025-09-09','2025-12-01'),('short',DAYS[0],DAYS[1]),*[(d,d,d) for d in DAYS],('relay',DAYS[0],DAYS[1])]:
        native=role!='relay';stamp=lambda d:int(datetime.fromisoformat(d).replace(tzinfo=CST).timestamp()*1000)
        params={'thscode':code,'interval':'1d','start':stamp(start),'end':stamp(end)+86400000-1,'adjust':'none','offset':0} if native else {'ts_code':code,'start_date':start.replace('-',''),'end_date':end.replace('-','')}
        requests.append({'role':role,'provider':'hithink_native' if native else 'xiaodefa_relay','api':probe.PRICES if native else 'daily','code':code,'start':start,'end':end,'params':params})
    return requests


def normalize(request,data):
    if request['provider']=='hithink_native':
        if any(data.get(k)!=v for k,v in {'thscode':request['code'],'interval':'1d','adjust':'none'}.items()):raise ValueError('native response identity/interval/adjustment differs')
    return probe.rows_for(request,data)


def replay_batch(folder,code,origin):
    folder=Path(folder);members=sealed(folder);reg=read_json(folder/'registration.json')[0];requests=plan(code)
    if reg['requests']!=requests or reg['origin']!=origin or reg['automatic_retries']!=0:raise ValueError('batch registration differs')
    required={'registration.json',*(f'status-{i}.json' for i in range(5))}
    if not required<=set(members) or not set(members)<=required|{f'receipt-{i}.json' for i in range(5)}:raise ValueError('batch membership differs')
    values={};statuses=[]
    for i,r in enumerate(requests):
        status=read_json(folder/f'status-{i}.json')[0];statuses.append(status)
        if status.get('index')!=i or status.get('status') not in ('observed','failed','skipped'):raise ValueError('invalid request status')
        name=f'receipt-{i}.json'
        if name in members:
            receipt=read_json(folder/name)[0]
            if receipt['request']!=r or not utc(reg['started_at'])<=utc(receipt['received_at'])<=now_utc():raise ValueError('receipt request/time differs')
        if status['status']=='observed':
            if name not in members:raise ValueError('observed receipt absent')
            rows=normalize(r,receipt['data'])
            if type(status.get('rows')) is not int or status['rows']!=len(rows):raise ValueError('row count differs')
            values[r['role']]={'rows':rows,'receipt_sha256':members[name],'received_at':receipt['received_at']}
    return members,values,statuses


def capture(analysis,output,*,limit=38,client=None):
    binding=parent(analysis);output=Path(output).resolve();source=Path(analysis).resolve()
    if type(limit) is not int or not 1<=limit<=38:raise ValueError('one to 38 codes required')
    if output==source or source in output.parents or output in source.parents:raise ValueError('separate new capture namespace required')
    codes=sorted(binding['cases'])[:limit];origin='native_and_relay' if client is None else 'synthetic_fixture'
    expected={'scope':SCOPE,'analysis_sha256':binding['analysis_sha256'],'codes':codes,'max_requests':5*len(codes),'automatic_retries':0,'origin':origin,'execution_ready':False}
    source_sha=file_hash(Path(__file__))
    if output.exists():
        reg=read_json(output/'registration.json')[0]
        if {k:reg.get(k) for k in expected}!=expected:raise ValueError('capture registration changed')
        if (output/'failed.json').exists():raise ValueError('failed initialization requires a separate explicit attempt')
        if not {p.name for p in output.iterdir()}<={'registration.json','completed.json',*codes}:raise ValueError('capture membership differs')
        for code in codes:
            if (output/code).exists():replay_batch(output/code,code,origin)
    else:
        output.mkdir(parents=True)
        write_json(output/'registration.json',{**expected,'source_sha256':source_sha,'started_at':now_utc().isoformat()})
    # No client initialization when resuming an already complete package.
    pending=[c for c in codes if not (output/c/'completed.json').exists()]
    failures=Counter()
    if pending:
        try:client=client or probe.Client()
        except Exception as exc:
            write_json(output/'failed.json',{'phase':'client_initialization','error_type':type(exc).__name__})
            raise ValueError('capture client initialization failed; details withheld') from None
    for code in codes:
        batch=output/code
        if batch.exists():
            _,_,statuses=replay_batch(batch,code,origin)
        else:
            batch.mkdir();requests=plan(code)
            write_json(batch/'registration.json',{'requests':requests,'origin':origin,'automatic_retries':0,'started_at':now_utc().isoformat()})
            statuses=[]
            for i,r in enumerate(requests):
                status={'index':i}
                if failures[r['provider']]>=3:status.update(status='skipped',reason='three_consecutive_provider_failures')
                else:
                    try:
                        time.sleep(.75);data=client.query(r)
                        if len(canonical(data).encode())>3_900_000:raise ValueError('response budget exceeded')
                        write_json(batch/f'receipt-{i}.json',{'request':r,'data':data,'received_at':now_utc().isoformat()})
                        rows=normalize(r,data);status.update(status='observed',rows=len(rows))
                    except Exception as exc:status.update(status='failed',error_type=type(exc).__name__,http_status=getattr(exc,'code',None))
                statuses.append(status);write_json(batch/f'status-{i}.json',status)
                if status['status']=='failed':failures[r['provider']]+=1
                elif status['status']=='observed':failures[r['provider']]=0
            seal(batch)
            print(canonical({'code':code,'statuses':dict(Counter(s['status'] for s in statuses))}),flush=True)
            continue
        for r,s in zip(plan(code),statuses):
            if s['status']=='failed':failures[r['provider']]+=1
            elif s['status']=='observed':failures[r['provider']]=0
    manifests={c:identity(replay_batch(output/c,c,origin)[0]) for c in codes}
    if binding!=parent(analysis) or source_sha!=file_hash(Path(__file__)):raise ValueError('capture inputs changed')
    completed={'registration_sha256':file_hash(output/'registration.json'),'batch_manifest_ids':manifests}
    if (output/'completed.json').exists():
        if read_json(output/'completed.json')[0]!=completed:raise ValueError('completed capture differs')
    else:write_json(output/'completed.json',completed)


def compare(cases,values):
    rows=[]
    for case in cases:
        day=case['date'];roles=['wide','short',day,'relay']
        observed={role:values.get(role,{}).get('rows',{}).get(day) for role in roles}
        complete=all(observed.values())
        old=next(e['values'] for e in case['evidence'] if e['provider']=='hithink_native')
        def equal(a,b,*,cross=False):
            if a is None or b is None:return False
            tolerances={'open':'0.00000001','high':'0.00000001','low':'0.00000001','close':'0.00000001','volume_shares':'0.000001','turnover_cny':'.50'}
            return all(abs(number(a[k])-number(b[k]))<=(number(t) if cross else 0) for k,t in tolerances.items())
        invariant=complete and equal(observed['wide'],observed['short']) and equal(observed['wide'],observed[day])
        corroborated=invariant and equal(observed['wide'],observed['relay'],cross=True)
        status=('incomplete_observations' if not complete else 'native_window_inconsistent' if not invariant else
                'dual_source_now_agrees_revision_review_required' if corroborated else 'persistent_cross_source_conflict')
        rows.append({'date':day,'parent_record_id':case['parent_record_id'],'status':status,'observations':observed,
            'historical_native_observation':old,'native_changed_since_parent':None if observed['wide'] is None else not equal(old,observed['wide']),
            'native_window_invariant':invariant,'dual_source_now_agrees':corroborated,'canonical_replacement_authorized':False,
            'research_ready':False,'execution_ready':False})
    return rows


def numeric_differences(rows):
    """Count Decimal value differences, never formatting such as 100 vs 100.0."""
    fields=('open','high','low','close','volume_shares','turnover_cny')
    counts={k:0 for k in fields};complete=0;missing=0
    for row in rows:
        observations=row['observations'];native=observations.get('wide');relay=observations.get('relay')
        if native is None or relay is None:
            missing+=1;continue
        complete+=1
        for key in fields:counts[key]+=number(native[key])!=number(relay[key])
    return {'complete_pairs':complete,'missing_pairs':missing,'exact_numeric_differences':counts,
            'comparison':'Decimal_exact_not_string_representation','repair_authorized':False}


def analyze(analysis,receipts,output):
    binding=parent(analysis);receipts=Path(receipts);output=Path(output).resolve();reg=read_json(receipts/'registration.json')[0]
    if output.exists() or any(output==p.resolve() or p.resolve() in output.parents or output in p.resolve().parents for p in (Path(analysis),receipts)):raise ValueError('separate new output required')
    codes=reg['codes']
    if (reg['scope']!=SCOPE or reg['origin']!='native_and_relay' or reg['analysis_sha256']!=binding['analysis_sha256']
        or not codes or codes!=sorted(binding['cases'])[:len(codes)] or reg['max_requests']!=len(codes)*5 or reg['automatic_retries']!=0 or reg['execution_ready'] is not False):raise ValueError('exact real capture binding required')
    if {p.name for p in receipts.iterdir()}!={'registration.json','completed.json',*codes}:raise ValueError('capture membership differs')
    manifests={};reports={};counts=Counter();statuses=Counter()
    for code in codes:
        members,values,state=replay_batch(receipts/code,code,reg['origin']);manifests[code]=identity(members)
        reports[code]=compare(binding['cases'][code],values);counts.update(r['status'] for r in reports[code]);statuses.update(s['status'] for s in state)
    completed={'registration_sha256':file_hash(receipts/'registration.json'),'batch_manifest_ids':manifests}
    if read_json(receipts/'completed.json')[0]!=completed or parent(analysis)!=binding:raise ValueError('capture/parent changed')
    output.mkdir(parents=True)
    for code,rows in reports.items():write_json(output/(code+'.json'),{'code':code,'cases':rows,'receipt_manifest_id':manifests[code]})
    result={'scope':SCOPE,'source_sha256':file_hash(Path(__file__)),'analysis_sha256':binding['analysis_sha256'],'capture_binding':completed,
        'codes':len(codes),'cases':sum(counts.values()),'case_statuses':dict(counts),'request_statuses':dict(statuses),
        'numeric_differences':numeric_differences([r for rows in reports.values() for r in rows]),
        'canonical_replacements':0,'research_ready':False,'execution_ready':False,'production_cutover':False}
    write_json(output/'result.json',result);seal(output);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['capture','analyze']);p.add_argument('--analysis',required=True);p.add_argument('--output',required=True);p.add_argument('--receipts');p.add_argument('--limit',type=int,default=38)
    a=p.parse_args()
    if a.action=='capture':capture(a.analysis,a.output,limit=a.limit)
    else:print(canonical(analyze(a.analysis,a.receipts,a.output)))
