"""Fixed, checkpointed dual-source evidence for all 2025 duplicate keys, no raw DB repair."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime
from pathlib import Path
import re
import sys
import time

import duckdb

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2 import probe_identity_sources as probe
from tools.v2.normalize_price_units import qualify,FIELDS
from tools.v2.canonical_price_research import resolve
from trade_system.v2.daily_session import CST,seal
from trade_system.v2.domain import canonical,file_hash,identity,now_utc,utc
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.research_receipts import sealed

WINDOWS=(('2025-01-01','2025-03-24'),('2025-03-25','2025-06-16'),('2025-06-17','2025-09-08'),('2025-09-09','2025-12-01'),('2025-12-02','2025-12-31'))
SCOPE='all_frozen_2025_duplicate_keys_native_relay_observations_not_PIT_or_production_repair'
DUP="SELECT stock_code,date FROM tushare_daily WHERE date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY stock_code,date HAVING count(*)>1"


def inventory(db):
    with duckdb.connect(str(db),read_only=True) as con:
        keys=con.execute(DUP+' ORDER BY stock_code,date').fetchall()
        pairs=con.execute('SELECT DISTINCT stock_code,ts_code FROM tushare_daily WHERE stock_code IN (SELECT stock_code FROM ('+DUP+")) AND date BETWEEN '2025-01-01' AND '2025-12-31' ORDER BY stock_code,ts_code").fetchall()
    codes={}
    for ticker,alias in pairs:
        if not re.fullmatch(r'\d{6}',ticker):raise ValueError('explicit six digit code required')
        forms=[ticker+'.SZ','SZ.'+ticker,ticker+'.SH','SH.'+ticker]
        if alias not in forms:raise ValueError('unsupported/conflicting stored identity')
        code=ticker+('.SZ' if 'SZ' in alias else '.SH')
        if ticker in codes and codes[ticker]!=code:raise ValueError('ambiguous exchange')
        codes[ticker]=code
    if not 1<=len(codes)<=54 or len(keys)>14000:raise ValueError('bounded pre-audited duplicate scope required')
    return {'codes':sorted(codes.values()),'duplicate_keys':len(keys),'key_digest':identity([[c,str(d)] for c,d in keys])}


def plan(codes):
    if not 1<=len(codes)<=6 or len(set(codes))!=len(codes) or any(not re.fullmatch(r'\d{6}\.(SH|SZ)',c) for c in codes):raise ValueError('bounded exact batch codes required')
    requests=[]
    for code in codes:
        for start,end in WINDOWS:
            for provider in ('hithink_native','xiaodefa_relay'):
                stamp=lambda d:int(datetime.fromisoformat(d).replace(tzinfo=CST).timestamp()*1000)
                params={'thscode':code,'interval':'1d','start':stamp(start),'end':stamp(end)+86400000-1,'adjust':'none','offset':0} if provider=='hithink_native' else {'ts_code':code,'start_date':start.replace('-',''),'end_date':end.replace('-','')}
                requests.append({'provider':provider,'api':probe.PRICES if provider=='hithink_native' else 'daily','code':code,'start':start,'end':end,'params':params})
    return requests


def capture(db,output,*,client=None):
    output=Path(output);observed=inventory(db);dbsha=file_hash(db)
    expected={'scope':SCOPE,'database_sha256':dbsha,**observed,'max_requests':len(observed['codes'])*10,'retries':0,'batch_size':6,
        'origin':'native_and_relay' if client is None else 'synthetic_fixture','execution_ready':False}
    if output.exists():
        registration=read_json(output/'registration.json')[0]
        if {k:registration[k] for k in expected}!=expected:raise ValueError('resume registration differs')
    else:
        output.mkdir(parents=True,exist_ok=False)
        write_json(output/'registration.json',{**expected,'started_at':now_utc().isoformat(),'capture_source_sha256':file_hash(Path(__file__))})
    client=client or probe.Client()
    for batch,offset in enumerate(range(0,len(observed['codes']),6),1):
        target=output/f'batch-{batch:02d}';codes=observed['codes'][offset:offset+6]
        if target.exists():
            replay_batch(target,codes,expected['origin']);continue
        target.mkdir();requests=plan(codes);failures=Counter()
        write_json(target/'registration.json',{'codes':codes,'requests':requests,'origin':expected['origin'],'started_at':now_utc().isoformat(),'retries':0})
        for i,r in enumerate(requests):
            status={'index':i}
            if failures[r['provider']]>=5:status.update(status='skipped',reason='five_consecutive_provider_failures')
            else:
                try:
                    time.sleep(.35);data=client.query(r)
                    if len(canonical(data).encode())>3_900_000:raise ValueError('response budget exceeded')
                    write_json(target/f'receipt-{i:02d}.json',{'request':r,'received_at':now_utc().isoformat(),'data':data})
                    values=probe.rows_for(r,data);status.update(status='observed',rows=len(values));failures[r['provider']]=0
                except Exception as exc:
                    failures[r['provider']]+=1;status.update(status='failed',error_type=type(exc).__name__,http_status=getattr(exc,'code',None))
            write_json(target/f'status-{i:02d}.json',status)
        seal(target);print(canonical({'batch':batch,'codes':codes,'request_statuses':dict(Counter(read_json(target/f'status-{i:02d}.json')[0]['status'] for i in range(len(requests))))}),flush=True)
    if dbsha!=file_hash(db):raise ValueError('source DB changed')


def replay_batch(folder,codes,origin):
    members=sealed(folder);reg=read_json(folder/'registration.json')[0];requests=plan(codes)
    if reg['codes']!=codes or reg['requests']!=requests or reg['retries']!=0 or reg['origin']!=origin:raise ValueError('batch registration differs')
    required={'registration.json',*(f'status-{i:02d}.json' for i in range(len(requests)))}
    if not required<=set(members) or not set(members)<=required|{f'receipt-{i:02d}.json' for i in range(len(requests))}:raise ValueError('batch membership differs')
    evidence=defaultdict(list);statuses=[]
    for i,r in enumerate(requests):
        status=read_json(folder/f'status-{i:02d}.json')[0];statuses.append(status)
        if status.get('index')!=i or status.get('status') not in ('observed','failed','skipped'):raise ValueError('bad batch status')
        filename=f'receipt-{i:02d}.json'
        if filename in members:
            receipt=read_json(folder/filename)[0]
            if receipt['request']!=r or not utc(reg['started_at'])<=utc(receipt['received_at'])<=now_utc():raise ValueError('receipt identity/time mismatch')
        if status['status']=='observed':
            if filename not in members:raise ValueError('observed receipt absent')
            rows=probe.rows_for(r,receipt['data'])
            if status['rows']!=len(rows):raise ValueError('row count changed')
            for day,values in rows.items():
                evidence[(r['code'].split('.')[0],day)].append({'provider':r['provider'],'request_code':r['code'],'date':day,'values':values,
                    'receipt_file':folder.name+'/'+filename,'receipt_sha256':members[filename],'received_at':receipt['received_at']})
    return members,evidence,statuses


def analyze(db,receipts,output,*,repair=None):
    receipts=Path(receipts);output=Path(output)
    if output.exists():raise ValueError('new output required')
    reg=read_json(receipts/'registration.json')[0];dbsha=file_hash(db);observed=inventory(db)
    if reg['scope']!=SCOPE or reg['database_sha256']!=dbsha or reg['origin']!='native_and_relay' or reg['retries']!=0 or reg['max_requests']!=len(observed['codes'])*10 or reg['batch_size']!=6 or reg['execution_ready'] is not False or any(reg[k]!=v for k,v in observed.items()):raise ValueError('exact real source registration required')
    evidence={};manifests={};statuses=[]
    for batch,offset in enumerate(range(0,len(observed['codes']),6),1):
        name=f'batch-{batch:02d}'
        members,rows,state=replay_batch(receipts/name,observed['codes'][offset:offset+6],reg['origin'])
        manifests[name]=identity(members);evidence.update(rows);statuses.extend(state)
    repair_members=None;repair_counts=None
    if repair is not None:
        from tools.v2.repair_year_campaign import replay as replay_repair
        repair_members,additional,repair_counts=replay_repair(receipts,repair)
        for key,values in additional.items():evidence.setdefault(key,[]).extend(values)
    qualified=[]
    with duckdb.connect(str(db),read_only=True) as con:
        rows=con.execute('SELECT '+','.join('d.'+f for f in FIELDS)+' FROM tushare_daily d JOIN ('+DUP+') x ON d.stock_code=x.stock_code AND d.date=x.date ORDER BY d.stock_code,d.date,d.ts_code').fetchall()
    exchanges={c.split('.')[0]:c.split('.')[1] for c in observed['codes']}
    for record in rows:
        original={k:None if v is None else str(v) for k,v in zip(FIELDS,record)}
        qualified.append(qualify(original,evidence.get((original['stock_code'],original['date']),[]),exchange=exchanges[original['stock_code']],amount_tolerance='.50'))
    resolved=resolve(qualified)
    if len(resolved)!=observed['duplicate_keys'] or dbsha!=file_hash(db):raise ValueError('source scope changed')
    if manifests!={name:identity(sealed(receipts/name)) for name in manifests}:raise ValueError('receipt package changed')
    if repair is not None and repair_members!=sealed(repair):raise ValueError('repair package changed')
    result={'scope':SCOPE,'source_sha256':file_hash(Path(__file__)),'database_sha256':dbsha,'batch_manifest_ids':manifests,**observed,
        'request_statuses':dict(Counter(s['status'] for s in statuses)),'repair_counts':repair_counts,
        'repair_manifest_id':identity(repair_members) if repair_members is not None else None,'raw_source_rows':len(rows),
        'qualified_raw_rows':sum(r['status']=='observed_row_unit_qualified' for r in qualified),
        'canonical_duplicate_keys':sum(r['status']=='canonical_price_observation' for r in resolved),
        'quarantined_duplicate_keys':sum(r['status']=='quarantined' for r in resolved),
        'unqualified_reasons':dict(Counter(r['reason'] for r in qualified if r['status']!='observed_row_unit_qualified')),
        'records':resolved,'raw_records_deleted':0,'source_unchanged':True,'research_ready':False,'execution_ready':False,'production_cutover':False}
    output.mkdir(parents=True,exist_ok=False);write_json(output/'result.json',result);seal(output)
    return {k:v for k,v in result.items() if k not in ('records','codes','batch_manifest_ids')}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['capture','analyze']);p.add_argument('--db',required=True);p.add_argument('--output',required=True);p.add_argument('--receipts');p.add_argument('--repair')
    a=p.parse_args()
    if a.action=='capture':capture(a.db,a.output)
    else:print(canonical(analyze(a.db,a.receipts,a.output,repair=a.repair)))
