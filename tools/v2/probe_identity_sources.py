"""Bounded old/new code source comparison. Never authorizes a historical merge."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import urllib.request

import duckdb

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.daily_session import CST,seal
from trade_system.v2.domain import canonical,file_hash,identity,now_utc,number,utc
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.research_receipts import FIELDS,Relay,sealed
from trade_system.v2.native_enrichment import PRICES

API_FIELDS={'daily':['ts_code','trade_date','open','high','low','close','vol','amount'],**FIELDS}
WINDOWS=[('2024-01-02','2024-02-22'),('2025-02-10','2025-02-21')]
CODES=['300114.SZ','302132.SZ']


def plan():
    result=[]
    for start,end in WINDOWS:
        for code in CODES:
            stamp=lambda d:int(datetime.fromisoformat(d).replace(tzinfo=CST).timestamp()*1000)
            result.append({'provider':'hithink_native','api':PRICES,'code':code,'start':start,'end':end,
              'params':{'thscode':code,'interval':'1d','start':stamp(start),'end':stamp(end)+86400000-1,'adjust':'none','offset':0}})
    for start,end in WINDOWS:
        for code in CODES:
            for api in API_FIELDS:
                result.append({'provider':'xiaodefa_relay','api':api,'code':code,'start':start,'end':end,
                  'params':{'ts_code':code,'start_date':start.replace('-',''),'end_date':end.replace('-','')}})
    return result


class Client:
    def __init__(self):
        from trade_system.hithink_client import HiThinkClient
        self.native=HiThinkClient(timeout=15,max_response_bytes=4_000_000,single_attempt=True)
        self.relay=None

    def query(self,request):
        if request['provider']=='hithink_native':return self.native._get(request['api'],request['params'])
        from trade_system.http_transport import open_verified_once
        if self.relay is None:self.relay=Relay()
        r=self.relay
        time.sleep(max(0,.65-(time.monotonic()-r.last)));r.last=time.monotonic()
        body={'api_name':request['api'],'token':r.token,'params':request['params'],'fields':','.join(API_FIELDS[request['api']])}
        req=urllib.request.Request(r.url,data=canonical(body).encode(),headers={'Content-Type':'application/json'})
        with open_verified_once(req,timeout=15) as response:raw=response.read(4_000_001)
        if len(raw)>4_000_000:raise ValueError('response budget exceeded')
        envelope=json.loads(raw)
        if envelope.get('code')!=0:raise ValueError('provider rejected request')
        return envelope.get('data') or {}


def rows_for(request,data, *, max_items=64):
    native=request['provider']=='hithink_native'
    items=data.get('item' if native else 'items')
    if not isinstance(max_items,int) or not 1<=max_items<=367: raise ValueError('bounded parser budget required')
    if not isinstance(items,list) or len(items)>=max_items:raise ValueError('bounded response required; possible truncation refused')
    if not native and data.get('fields')!=API_FIELDS[request['api']]:raise ValueError('exact fields required')
    result={}
    for item in items:
        if native:
            day=datetime.fromtimestamp(float(number(item['date_ms']))/1000,tz=CST).date().isoformat()
            row={k:str(number(item[k+'_price'])) for k in ('open','high','low','close')}
            row.update(volume_shares=str(number(item['volume'])),turnover_cny=str(number(item['turnover'])))
        else:
            if len(item)!=len(API_FIELDS[request['api']]):raise ValueError('row width differs')
            source=dict(zip(API_FIELDS[request['api']],item))
            if source['ts_code']!=request['code']:raise ValueError('provider returned another code; no implicit alias')
            day=datetime.strptime(source['trade_date'],'%Y%m%d').date().isoformat()
            row={k:(None if v is None else str(number(v))) for k,v in source.items() if k not in ('ts_code','trade_date')}
            if request['api']=='daily':
                row['volume_shares']=str(number(row.pop('vol'))*100)
                row['turnover_cny']=str(number(row.pop('amount'))*1000)
            elif request['api']=='adj_factor' and (row['adj_factor'] is None or number(row['adj_factor'])<=0):
                raise ValueError('positive factor required')
            elif request['api']=='moneyflow' and any(v is not None and number(v)<0 for k,v in row.items() if k.startswith(('buy_','sell_'))):
                raise ValueError('nonnegative gross flow required')
        if not request['start']<=day<=request['end'] or day in result:raise ValueError('duplicate or out-of-window date')
        if native or request['api']=='daily':
            o,h,l,c=[number(row[k]) for k in ('open','high','low','close')]
            if min(o,h,l,c)<=0 or l>min(o,c) or h<max(o,c) or number(row['volume_shares'])<0 or number(row['turnover_cny'])<0:
                raise ValueError('invalid price/volume bounds')
        result[day]=row
    return result


def capture(output,client=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    requests=plan()
    registration={'scope':'retrospective_identity_source_probe_not_merge_authority','origin':'native_and_relay' if client is None else 'synthetic_fixture',
        'requests':requests,'max_requests':len(requests),'retries':0,'received_after':now_utc().isoformat(),
        'source_sha256':file_hash(Path(__file__)),'execution_ready':False}
    write_json(output/'registration.json',registration)
    client=client or Client()
    for i,request in enumerate(requests):
        try:
            data=client.query(request)
            if len(canonical(data).encode())>3_900_000:raise ValueError('bounded stored response required')
            receipt={'request':request,'received_at':now_utc().isoformat(),'data':data}
            # Retain unexpected successful response before normalization rejects it.
            write_json(output/f'receipt-{i:02d}.json',receipt)
            parsed=rows_for(request,data)
            status={'index':i,'status':'observed','rows':len(parsed)}
        except Exception as exc:
            status={'index':i,'status':'failed','error_type':type(exc).__name__}
        write_json(output/f'status-{i:02d}.json',status)
        print(json.dumps(status),flush=True)
    seal(output)


def analyze(folder,db,output):
    source_hash=file_hash(Path(__file__))
    folder=Path(folder);members=sealed(folder);reg=read_json(folder/'registration.json')[0]
    if (reg['requests']!=plan() or reg['max_requests']!=16 or reg['retries']!=0
        or reg['scope']!='retrospective_identity_source_probe_not_merge_authority'
        or reg['origin'] not in ('native_and_relay','synthetic_fixture') or reg['execution_ready'] is not False):
        raise ValueError('probe request plan changed')
    required={'registration.json',*(f'status-{i:02d}.json' for i in range(16))}
    optional={f'receipt-{i:02d}.json' for i in range(16)}
    if not required<=set(members) or not set(members)<=required|optional:raise ValueError('receipt membership changed')
    parsed={};statuses=[]
    for i,req in enumerate(plan()):
        status=read_json(folder/f'status-{i:02d}.json')[0];statuses.append({**req,**status})
        if status['index']!=i or status['status'] not in ('observed','failed'):raise ValueError('invalid request status')
        if (folder/f'receipt-{i:02d}.json').exists():
            receipt=read_json(folder/f'receipt-{i:02d}.json')[0]
            if receipt['request']!=req or not utc(reg['received_after'])<=utc(receipt['received_at'])<=now_utc():
                raise ValueError('request binding/time changed')
        if status['status']=='observed':
            receipt=read_json(folder/f'receipt-{i:02d}.json')[0]
            if receipt['request']!=req:raise ValueError('request binding changed')
            rows=rows_for(req,receipt['data'])
            if len(rows)!=status['rows']:raise ValueError('row count changed')
            parsed[i]=rows
    db_hash=file_hash(db);comparisons=[]
    with duckdb.connect(str(db),read_only=True) as c:
        for i,req in enumerate(plan()):
            if i not in parsed or (req['provider']!='hithink_native' and req['api']!='daily'):continue
            stored={str(r[0]):r[1:] for r in c.execute('SELECT date,open,high,low,close,volume,turnover,volume_unit,amount_unit FROM tushare_daily WHERE stock_code=? AND date BETWEEN ? AND ?',["302132",req['start'],req['end']]).fetchall()}
            for day,row in parsed[i].items():
                old=stored.get(day)
                if old is None:continue
                errors={k:float(number(row[k])-number(old[j])) for j,k in enumerate(('open','high','low','close'))}
                comparisons.append({'request_index':i,'request_code':req['code'],'provider':req['provider'],'date':day,
                    'stored_code':'302132','ohlc_deltas':errors,
                    'volume_source_over_stored':float(number(row['volume_shares'])/number(old[4])) if old[4] else None,
                    'amount_source_over_stored':float(number(row['turnover_cny'])/number(old[5])) if old[5] else None,
                    'stored_volume_unit':old[6],'stored_amount_unit':old[7]})
    # Exact returned date sets, missingness and identities stay visible per endpoint.
    summaries=[];pairs=[]
    for i,req in enumerate(plan()):
        summaries.append({'index':i,'provider':req['provider'],'api':req['api'],'code':req['code'],'window':[req['start'],req['end']],
            'status':statuses[i]['status'],'dates':sorted(parsed.get(i,{})),
            'error_type':statuses[i].get('error_type'),
            'raw_receipt_present':f'receipt-{i:02d}.json' in members,
            'money_amount_unit':'ten_thousand_CNY' if req['api']=='moneyflow' else None,
            'normalized_rows':parsed.get(i,{})})
    for left,right in [(4,7),(5,8),(6,9),(10,13),(11,14),(12,15),(1,4),(1,7),(3,10),(3,13)]:
        a,b=parsed.get(left,{}),parsed.get(right,{})
        common=sorted(set(a)&set(b));mismatch=0;max_delta={}
        for day in common:
            for field in set(a[day])|set(b[day]):
                x,y=a[day].get(field),b[day].get(field)
                if x is None or y is None:
                    mismatch+=int(x!=y);continue
                delta=abs(number(x)-number(y));mismatch+=int(delta>0)
                max_delta[field]=max(max_delta.get(field,0),float(delta))
        pairs.append({'left':left,'right':right,'both_observed':left in parsed and right in parsed,
            'shared_dates':common,'left_only':sorted(set(a)-set(b)),'right_only':sorted(set(b)-set(a)),
            'field_mismatches':mismatch,'max_absolute_delta':max_delta,
            'complete_equal_observed_window':bool(a) and a==b})
    unit_checks=[]
    for row in comparisons:
        v=row['volume_source_over_stored'];a=row['amount_source_over_stored']
        unit_checks.append({'request_index':row['request_index'],'date':row['date'],
            'volume_declared_scale':100 if row['stored_volume_unit']=='hands' else None,
            'amount_declared_scale':1000 if row['stored_amount_unit']=='thousand_yuan' else None,
            'volume_observed_scale':v,'amount_observed_scale':a,
            'declared_units_match':(row['stored_volume_unit']=='hands' and v is not None and abs(v-100)<1e-4
                and row['stored_amount_unit']=='thousand_yuan' and a is not None and abs(a-1000)<1e-4)})
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    result={'origin':reg['origin'],'receipt_manifest_id':identity(members),'source_sha256':source_hash,
        'source_unchanged':source_hash==file_hash(Path(__file__)),
        'database_sha256':db_hash,'database_unchanged':db_hash==file_hash(db),'receipt_unchanged':members==sealed(folder),
        'requests':summaries,'price_comparisons':comparisons,'paired_source_checks':pairs,'unit_checks':unit_checks,
        'unit_qualification':'failed_or_unknown' if not unit_checks or not all(x['declared_units_match'] for x in unit_checks) else 'sample_only_not_global_qualification',
        'merge_authorized':False,'research_ready':False,'execution_ready':False}
    write_json(output/'result.json',result)
    if not result['database_unchanged'] or not result['receipt_unchanged'] or not result['source_unchanged']:raise ValueError('probe inputs changed')
    return {'requests':len(summaries),'observed_requests':len(parsed),'price_comparisons':len(comparisons),
            'ohlc_mismatch_rows':sum(any(abs(v)>1e-8 for v in r['ohlc_deltas'].values()) for r in comparisons),
            'receipt_manifest_id':result['receipt_manifest_id'],'merge_authorized':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['capture','analyze'])
    p.add_argument('--output',required=True);p.add_argument('--receipts');p.add_argument('--db');a=p.parse_args()
    if a.action=='capture':capture(a.output)
    else:print(json.dumps(analyze(a.receipts,a.db,a.output)))
