"""Bounded six-month native/relay research campaign. No retrospective PIT promotion."""
import argparse
from datetime import date, datetime, timedelta
from collections import Counter
from pathlib import Path
import sys
import time
import json
import urllib.request

import duckdb

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2 import probe_identity_sources as probe
from trade_system.v2.daily_session import CALENDAR,CST,seal
from trade_system.v2.domain import canonical,file_hash,identity,now_utc,number,utc
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.research_receipts import sealed,Relay

CODES=['000001.SZ','002767.SZ','600276.SH','600278.SH']
WINDOWS=[('2025-01-01','2025-03-24'),('2025-03-25','2025-06-16'),('2025-06-17','2025-06-30')]
FIELDS={**probe.API_FIELDS,'trade_cal':['exchange','cal_date','is_open','pretrade_date'],
        'stock_basic':['ts_code','symbol','name','exchange','market','list_status','list_date','delist_date']}
SCOPE='frozen_six_month_four_security_research_input_not_PIT_or_execution'
CONFIGURED_SCOPE='configured_bounded_research_input_not_PIT_or_execution'


def configuration(value=None):
    """Frozen bounded ranges, with the original four-stock protocol retained."""
    if value is None: return CODES, WINDOWS
    from datetime import timedelta
    import re
    if set(value) != {'codes','start','end','window_days','max_requests','selection_scope'}:
        raise ValueError('exact research capture configuration required')
    codes=value['codes']; start=date.fromisoformat(value['start']); end=date.fromisoformat(value['end'])
    if (not isinstance(codes,list) or not 1<=len(codes)<=64 or codes!=sorted(set(codes))
        or any(not re.fullmatch(r'\d{6}\.(SZ|SH)',c) for c in codes)
        or not 1<=value['window_days']<=90 or not 0<=(end-start).days<=366
        or not 1<=value['max_requests']<=600 or not value['selection_scope']):
        raise ValueError('bounded explicit capture universe/window/budget required')
    windows=[]; cursor=start
    while cursor<=end:
        stop=min(end,cursor+timedelta(days=value['window_days']-1))
        windows.append((cursor.isoformat(),stop.isoformat())); cursor=stop+timedelta(days=1)
    if 3+len(codes)*(4*len(windows)+1)>value['max_requests']:
        raise ValueError('capture request budget insufficient before network')
    return codes,windows


def plan(config=None):
    codes,windows=configuration(config)
    requests=[{'provider':'hithink_native','api':CALENDAR,'params':{},'kind':'native_calendar'}]
    for provider in ('hithink_native','xiaodefa_relay'):
        for code in codes:
            for start,end in windows:
                apis=[probe.PRICES] if provider=='hithink_native' else ['daily','adj_factor','moneyflow']
                for api in apis:
                    stamp=lambda d:int(datetime.fromisoformat(d).replace(tzinfo=CST).timestamp()*1000)
                    params={'thscode':code,'interval':'1d','start':stamp(start),'end':stamp(end)+86400000-1,'adjust':'none','offset':0} if provider=='hithink_native' else {
                        'ts_code':code,'start_date':start.replace('-',''),'end_date':end.replace('-','')}
                    requests.append({'provider':provider,'api':api,'code':code,'start':start,'end':end,'params':params,'kind':'security_history'})
    for exchange in ('SSE','SZSE'):
        requests.append({'provider':'xiaodefa_relay','api':'trade_cal','params':{'exchange':exchange,'start_date':windows[0][0].replace('-',''),'end_date':windows[-1][1].replace('-','')},'kind':'calendar'})
    for code in codes:
        requests.append({'provider':'xiaodefa_relay','api':'stock_basic','params':{'ts_code':code},'kind':'identity_snapshot'})
    return requests


class Client:
    def __init__(self):
        self.native=probe.Client().native;self.relay=None
    def query(self,r):
        if r['provider']=='hithink_native':return self.native._get(r['api'],r['params'])
        from trade_system.http_transport import open_verified_once
        self.relay=self.relay or Relay();relay=self.relay
        time.sleep(max(0,.65-(time.monotonic()-relay.last)));relay.last=time.monotonic()
        request=urllib.request.Request(relay.url,data=canonical({'api_name':r['api'],'token':relay.token,'params':r['params'],'fields':','.join(FIELDS[r['api']])}).encode(),headers={'Content-Type':'application/json'})
        with open_verified_once(request,timeout=15) as response:raw=response.read(4_000_001)
        if len(raw)>4_000_000:raise ValueError('response budget exceeded')
        envelope=json.loads(raw)
        if envelope.get('code')!=0:raise ValueError('provider rejected request')
        return envelope.get('data') or {}


def parse(r,data):
    if r['kind']=='security_history':
        days=(date.fromisoformat(r['end'])-date.fromisoformat(r['start'])).days+1
        return probe.rows_for(r,data,max_items=max(64,days+1))
    if r['kind']=='native_calendar':
        rows=data.get('item')
        if not isinstance(rows,list) or not 1<=len(rows)<=500:raise ValueError('bounded native calendar required')
        days=[datetime.strptime(x['date'],'%Y%m%d').date().isoformat() for x in rows]
        if len(days)!=len(set(days)):raise ValueError('duplicate native calendar date')
        return sorted(days)
    if data.get('fields')!=FIELDS[r['api']] or not isinstance(data.get('items'),list) or len(data['items'])>=6000:
        raise ValueError('exact fields and bounded complete response required')
    rows=[]
    for values in data['items']:
        if len(values)!=len(FIELDS[r['api']]):raise ValueError('row width differs')
        rows.append(dict(zip(FIELDS[r['api']],values)))
    if r['kind']=='calendar':
        start=datetime.strptime(r['params']['start_date'],'%Y%m%d').date()
        end=datetime.strptime(r['params']['end_date'],'%Y%m%d').date()
        expected=[(start+timedelta(days=i)).strftime('%Y%m%d') for i in range((end-start).days+1)]
        if sorted(x['cal_date'] for x in rows)!=expected or any(x['exchange']!=r['params']['exchange'] or type(x['is_open']) is not int or x['is_open'] not in (0,1) for x in rows):
            raise ValueError('full exact exchange calendar required')
    else:
        if len(rows)!=1 or rows[0]['ts_code']!=r['params']['ts_code'] or rows[0]['symbol']!=r['params']['ts_code'].split('.')[0]:
            raise ValueError('exact returned identity required')
        if rows[0]['exchange']!=('SSE' if r['params']['ts_code'].endswith('.SH') else 'SZSE') or rows[0]['list_status'] not in ('L','D','P'):
            raise ValueError('explicit exchange and listing status required')
        datetime.strptime(rows[0]['list_date'],'%Y%m%d')
        if rows[0]['delist_date']:
            datetime.strptime(rows[0]['delist_date'],'%Y%m%d')
            if rows[0]['delist_date']<=rows[0]['list_date']:raise ValueError('invalid identity effective dates')
    return rows


def capture(output,*,client=None,config=None):
    requests=plan(config)
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    write_json(output/'registration.json',{'scope':CONFIGURED_SCOPE if config else SCOPE,'requests':requests,'config':config,'max_requests':len(requests),'retries':0,
        'origin':'native_and_relay' if client is None else 'synthetic_fixture','started_at':now_utc().isoformat(),
        'source_sha256':file_hash(Path(__file__)),'fallback_reason':'HiThink current calendar cannot certify 2025; native corporate actions are not daily factors; relay supplies explicitly separate history products',
        'execution_ready':False})
    client=client or Client(); failures=Counter()
    for i,r in enumerate(requests):
        if failures[r['provider']]>=3:
            write_json(output/f'status-{i:02d}.json',{'index':i,'status':'skipped','reason':'three_consecutive_source_failures'})
            continue
        try:
            data=client.query(r)
            if len(canonical(data).encode())>3_900_000:raise ValueError('stored response budget')
            write_json(output/f'receipt-{i:02d}.json',{'request':r,'received_at':now_utc().isoformat(),'data':data})
            parsed=parse(r,data);status={'index':i,'status':'observed','rows':len(parsed)};failures[r['provider']]=0
        except Exception as e:
            failures[r['provider']]+=1;status={'index':i,'status':'failed','error_type':type(e).__name__}
        write_json(output/f'status-{i:02d}.json',status);print(canonical(status),flush=True)
    seal(output)


def replay(folder):
    folder=Path(folder);members=sealed(folder);reg=read_json(folder/'registration.json')[0]
    requests=plan(reg.get('config'))
    accepted_scopes={SCOPE,CONFIGURED_SCOPE} if reg.get('config') else {SCOPE}
    if reg['scope'] not in accepted_scopes or reg['requests']!=requests or reg['max_requests']!=len(requests) or reg['retries']!=0 or reg['execution_ready'] is not False or reg['origin'] not in ('native_and_relay','synthetic_fixture'):
        raise ValueError('campaign registration differs')
    required={'registration.json',*(f'status-{i:02d}.json' for i in range(len(requests)))}
    optional={f'receipt-{i:02d}.json' for i in range(len(requests))}
    if not required<=set(members) or not set(members)<=required|optional:raise ValueError('campaign membership differs')
    data={};statuses=[]
    for i,r in enumerate(requests):
        status=read_json(folder/f'status-{i:02d}.json')[0];statuses.append(status)
        if status.get('index')!=i or status.get('status') not in ('observed','failed','skipped'):raise ValueError('invalid status')
        filename=f'receipt-{i:02d}.json'
        if filename in members:
            receipt=read_json(folder/filename)[0]
            if receipt['request']!=r or not utc(reg['started_at'])<=utc(receipt['received_at'])<=now_utc():raise ValueError('receipt binding/time differs')
        if status['status']=='observed':
            if filename not in members:raise ValueError('missing observed receipt')
            parsed=parse(r,receipt['data'])
            if status['rows']!=len(parsed):raise ValueError('response count changed')
            data[i]=parsed
    return reg,members,data,statuses


def derive(folder):
    reg,members,data,statuses=replay(folder)
    if reg['origin']!='native_and_relay':raise ValueError('synthetic campaign not research evidence')
    calendar={};identities={};history={};bindings={}
    for i,r in enumerate(plan(reg.get('config'))):
        if i not in data:continue
        if r['kind']=='calendar':calendar[r['params']['exchange']]=data[i]
        elif r['kind']=='identity_snapshot':identities[r['params']['ts_code']]=data[i][0]
        elif r['kind']=='security_history':
            dataset='native' if r['provider']=='hithink_native' else r['api']
            for day,row in data[i].items():
                key=(r['code'],day,dataset)
                if key in history:raise ValueError('overlapping campaign key')
                history[key]=row;bindings[key]=f'receipt-{i:02d}.json'
    if set(calendar)!={'SSE','SZSE'}:raise ValueError('both exchange calendars required')
    days={e:[datetime.strptime(r['cal_date'],'%Y%m%d').date().isoformat() for r in sorted(rows,key=lambda x:x['cal_date']) if r['is_open']] for e,rows in calendar.items()}
    output=[]
    for code in configuration(reg.get('config'))[0]:
        exchange='SSE' if code.endswith('.SH') else 'SZSE'
        for day in days[exchange]:
            native=history.get((code,day,'native'));relay=history.get((code,day,'daily'));factor=history.get((code,day,'adj_factor'));money=history.get((code,day,'moneyflow'))
            gaps=[]
            if not native or not relay:gaps.append('missing_dual_source_price')
            elif (any(abs(number(native[k])-number(relay[k]))>number('0.00000001') for k in ('open','high','low','close'))
                or abs(number(native['volume_shares'])-number(relay['volume_shares']))>number('0.000001')
                or abs(number(native['turnover_cny'])-number(relay['turnover_cny']))>number('.50')):gaps.append('source_price_conflict')
            if native and number(native['volume_shares'])<=0:gaps.append('nonpositive_volume')
            if not factor:gaps.append('daily_factor_missing')
            info=identities.get(code)
            if not info or info['list_date']>day.replace('-','') or (info['delist_date'] and info['delist_date']<=day.replace('-','')):gaps.append('identity_snapshot_missing_or_outside_listing')
            quote=None if gaps else {k:str(number(native[k])*number(factor['adj_factor'])) for k in ('open','high','low','close')}
            row={'datetime':day,'instrument':code.split('.')[0],'ts_code':code,
                **{k:float(quote[k]) if quote else None for k in ('open','high','low','close')},
                'volume':float(number(native['volume_shares'])/number(factor['adj_factor'])) if quote else None,
                'turnover':float(number(native['turnover_cny'])) if quote else None,
                'adj_factor':float(number(factor['adj_factor'])) if factor else None,
                'net_mf_amount':float(number(money['net_mf_amount'])*10000) if money and money['net_mf_amount'] is not None else None,
                'adjusted_price_target_ret':None,'adjusted_price_target_date':None,'label_next_ret':None,'label_date':None,
                'gaps':gaps,'receipt_files':{name:bindings.get((code,day,name)) for name in ('native','daily','adj_factor','moneyflow')}}
            output.append(row)
    bykey={(r['ts_code'],r['datetime']):r for r in output}
    for r in output:
        sessions=days['SSE' if r['ts_code'].endswith('.SH') else 'SZSE'];i=sessions.index(r['datetime'])
        if i+2<len(sessions):
            n,n2=[bykey[(r['ts_code'],sessions[i+j])] for j in (1,2)]
            if all(x['close'] is not None and not x['gaps'] for x in (r,n,n2)):
                r['adjusted_price_target_ret']=(n2['close']/n['open']-1)*100;r['adjusted_price_target_date']=n2['datetime']
    return {'scope':CONFIGURED_SCOPE if reg.get('config') else SCOPE,'receipt_manifest_id':identity(members),'request_statuses':statuses,
        'calendar':days,'identity_snapshots':identities,'rows':output,'coverage':{
            'security_days':len(output),'price_and_factor_rows':sum(r['close'] is not None for r in output),
            'adjusted_proxy_rows':sum(r['adjusted_price_target_ret'] is not None for r in output),
            'moneyflow_rows':sum(r['net_mf_amount'] is not None for r in output),'gap_counts':dict(Counter(g for r in output for g in r['gaps']))},
        'source_sha256':file_hash(Path(__file__)),'volume_unit':'reciprocal_adjusted_shares','turnover_unit':'CNY',
        'identity_scope':'current_relay_snapshot_not_historical_name_status_or_PIT',
        'retrospective_prices_only':True,'research_ready':False,'execution_ready':False,
        'formal_blockers':['historical_lifecycle_and_suspension_not_fully_authenticated','point_in_time_universe_not_proven','future_joint_samples_not_mature','not_a_validated_selection_strategy']}


def export(folder,output):
    output=Path(output)
    if output.exists():raise ValueError('new campaign output required')
    before=sealed(folder);result=derive(folder);output.mkdir(parents=True,exist_ok=False)
    write_json(output/'research-input.json',result)
    with duckdb.connect(':memory:') as con:
        con.execute("CREATE TABLE features AS SELECT value->>'datetime' AS datetime,value->>'instrument' AS instrument,"+
            ','.join(f"CAST(value->>'{k}' AS DOUBLE) AS {k}" for k in ('open','high','low','close','volume','turnover','adj_factor','net_mf_amount','adjusted_price_target_ret','label_next_ret'))+
            ",value->>'adjusted_price_target_date' AS adjusted_price_target_date,value->>'label_date' AS label_date FROM json_each(?)",[json.dumps(result['rows'])])
        con.execute("COPY (SELECT * FROM features ORDER BY datetime,instrument) TO '"+str(output/'features.parquet').replace("'","''")+"' (FORMAT PARQUET)")
    write_json(output/'features.metadata.json',{'label_version':'native_campaign_v8_adjusted_proxy_not_training_labels','feature_columns':['open','high','low','close','volume','turnover','net_mf_amount'],
        'label_column':'label_next_ret','volume_unit':result['volume_unit'],'turnover_unit':'CNY','adjustment_available':True,
        'artifact_hashes':{'features.parquet':file_hash(output/'features.parquet')},'receipt_manifest_id':result['receipt_manifest_id'],
        'coverage':result['coverage'],'research_ready':False,'execution_ready':False})
    if before!=sealed(folder):raise ValueError('campaign changed during export')
    seal(output);return result['coverage']


def reconcile(folder,db,output):
    from tools.v2.normalize_price_units import qualify,FIELDS as RAW_FIELDS
    from tools.v2.canonical_price_research import resolve
    reg,members,data,statuses=replay(folder)
    if reg['origin']!='native_and_relay':raise ValueError('native campaign required')
    before=file_hash(db);evidence={}
    for i,r in enumerate(plan()):
        if i not in data or r['kind']!='security_history' or r['api'] not in (probe.PRICES,'daily'):continue
        receipt=read_json(Path(folder)/f'receipt-{i:02d}.json')[0]
        for day,values in data[i].items():
            evidence.setdefault((r['code'].split('.')[0],day),[]).append({'provider':r['provider'],'date':day,'request_code':r['code'],'values':values,
                'receipt_file':f'receipt-{i:02d}.json','receipt_sha256':members[f'receipt-{i:02d}.json'],'received_at':receipt['received_at']})
    rows=[]
    with duckdb.connect(str(db),read_only=True) as con:
        con.execute('SET threads=2')
        for code in CODES:
            ticker,exchange=code.split('.')
            values=con.execute('SELECT '+','.join(RAW_FIELDS)+" FROM tushare_daily WHERE stock_code=? AND date BETWEEN '2025-01-01' AND '2025-06-30' ORDER BY date,ts_code",[ticker]).fetchall()
            if len(values)>400:raise ValueError('bounded scoped source rows required')
            for record in values:
                original={k:None if v is None else str(v) for k,v in zip(RAW_FIELDS,record)}
                rows.append(qualify(original,evidence.get((ticker,original['date']),[]),exchange=exchange,amount_tolerance='.50'))
    resolved=resolve(rows)
    if before!=file_hash(db) or members!=sealed(folder):raise ValueError('reconciliation inputs changed')
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    result={'scope':'campaign_observed_source_variants_not_database_repair','database_sha256':before,'receipt_manifest_id':identity(members),
        'source_rows':len(rows),'source_units_qualified':sum(r['status']=='observed_row_unit_qualified' for r in rows),
        'canonical_rows':sum(r['status']=='canonical_price_observation' for r in resolved),
        'reconciled_duplicate_groups':sum(r['status']=='canonical_price_observation' and r['source_variant_count']>1 for r in resolved),
        'quarantined_groups':sum(r['status']=='quarantined' for r in resolved),'records':resolved,
        'raw_records_deleted':0,'source_unchanged':True,'research_ready':False,'execution_ready':False}
    write_json(output/'result.json',result);seal(output)
    return {k:v for k,v in result.items() if k!='records'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['capture','export','reconcile']);p.add_argument('--output',required=True);p.add_argument('--receipts');p.add_argument('--db')
    p.add_argument('--config',help='Frozen capture ranges and request budget')
    a=p.parse_args()
    if a.action=='capture':capture(a.output,config=read_json(a.config)[0] if a.config else None)
    elif a.action=='reconcile':print(canonical(reconcile(a.receipts,a.db,a.output)))
    else:print(canonical(export(a.receipts,a.output)))
