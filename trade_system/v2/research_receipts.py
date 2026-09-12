"""Bounded historical repair receipts, isolated from prospective qualifications."""
from datetime import datetime
import json
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit

from .daily_session import seal, CST
from .domain import canonical,file_hash,identity,now_utc,number,utc,instrument
from .gap_evidence import read_json,write_json

FIELDS={'adj_factor':['ts_code','trade_date','adj_factor'],
        'moneyflow':['ts_code','trade_date','net_mf_amount','buy_lg_amount','sell_lg_amount','buy_elg_amount','sell_elg_amount']}

MISSING_2024=['2024-01-03','2024-02-26','2024-04-08','2024-07-02','2024-07-12',
              '2024-07-30','2024-08-16','2024-10-14','2024-11-01','2024-12-03']


def calendar_overlay(rows):
    """Pure parser for the existing frozen 2024 protocol; no collector dependency."""
    from datetime import timedelta
    expected={(datetime(2024,1,1)+timedelta(days=i)).date().isoformat() for i in range(366)}
    result={}
    for row in rows:
        day=datetime.strptime(row['cal_date'],'%Y%m%d').date().isoformat()
        if row['exchange']!='SSE' or type(row['is_open']) is not int or row['is_open'] not in (0,1) or day in result:
            raise ValueError('unique explicit SSE calendar dates/status required')
        result[day]=bool(row['is_open'])
    if set(result)!=expected:
        raise ValueError('full 2024 calendar coverage required, not observed-bar inference')
    return {'exchange':'SSE','calendar_days':366,'open_days':sorted(d for d,v in result.items() if v),
            'target_dates':{d:result[d] for d in MISSING_2024},
            'scope':'new_relay_calendar_receipt_not_original_PIT_or_SZSE_certification'}


def sealed(folder):
    folder=Path(folder).resolve(strict=True);paths=list(folder.iterdir())
    if any(not p.is_file() or p.is_symlink() or p.stat().st_size>8_000_000 for p in paths):
        raise ValueError('bounded flat sealed receipts required')
    members={p.name:file_hash(p) for p in paths if p.name!='completed.json'}
    if read_json(folder/'completed.json')[0]!={'members':members,'manifest_id':identity(members)}:
        raise ValueError('receipt package changed')
    return members


def normalize(api,day,data):
    fields=data.get('fields');items=data.get('items')
    if fields!=FIELDS[api] or not isinstance(items,list) or len(items)>=6000:
        raise ValueError('exact fields and fewer than 6000 rows required; possible truncation refused')
    rows=[];seen=set()
    for values in items:
        if len(values)!=len(fields):raise ValueError('row width mismatch')
        raw=dict(zip(fields,values));ticker,exchange=raw['ts_code'].split('.')
        instrument(exchange+'.'+ticker)
        if raw['trade_date']!=day.replace('-','') or ticker in seen:
            raise ValueError('exact session and unique instrument required')
        seen.add(ticker);row={'date':day,'stock_code':ticker,'ts_code':raw['ts_code']}
        for field in fields[2:]:
            value=raw[field]
            if value is None:
                if api=='adj_factor':raise ValueError('positive daily factor required')
                row[field]=None;continue
            if isinstance(value,bool):raise ValueError('numeric value cannot be boolean')
            value=number(value)
            if abs(value)>10**15 or (field=='adj_factor' and value<=0) or (field.startswith(('buy_','sell_')) and value<0):
                raise ValueError('invalid factor or gross flow value')
            row[field]=str(value if api=='adj_factor' else value*10000)
        rows.append(row)
    return sorted(rows,key=lambda r:r['stock_code'])


class Relay:
    def __init__(self):
        from trade_system.config import SETTINGS
        self.token=SETTINGS.get('XIAODEFA_TOKEN') or SETTINGS.get('TUSHARE_XIAODEFA_TOKEN')
        self.url=SETTINGS.get('XIAODEFA_URL') or 'https://t.xiaodefa.top/'
        url=urlsplit(self.url)
        if not self.token or url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError('configured HTTPS xiaodefa endpoint and credential required')
        self.last=0

    def query(self,api,day):
        from trade_system.http_transport import open_verified_once
        time.sleep(max(0,.65-(time.monotonic()-self.last)))
        self.last=time.monotonic()
        body={'api_name':api,'token':self.token,'params':{'trade_date':day.replace('-','')},'fields':','.join(FIELDS[api])}
        req=urllib.request.Request(self.url,data=canonical(body).encode(),headers={'Content-Type':'application/json'})
        with open_verified_once(req,timeout=15) as response:raw=response.read(8_000_001)
        if len(raw)>8_000_000:raise ValueError('response byte budget exceeded')
        response=json.loads(raw)
        if response.get('code')!=0:raise ValueError('provider rejected request')
        return response.get('data') or {}


def capture(calendar,start,end,output,*,client=None,clock=now_utc):
    members=sealed(calendar)
    cal=read_json(Path(calendar)/'calendar-overlay.json')[0]
    # Revalidate the calendar's native receipt derivation, not just its seal.
    receipt=read_json(Path(calendar)/'calendar-relay-receipt.json')[0]
    if cal!={**calendar_overlay(receipt['rows']),'receipt_sha256':identity(receipt)}:
        raise ValueError('calendar derivation changed')
    days=cal['open_days'];selected=[d for d in days if start<=d<=end]
    if not selected or start!=selected[0] or end!=selected[-1]:raise ValueError('explicit open start/end required')
    stop=days.index(end)+3
    requested=days[days.index(start):stop]
    if len(requested)!=len(selected)+2 or len(requested)>10:raise ValueError('at most ten sessions including exact T+2 required')
    if utc(requested[-1]+'T16:00:00+08:00')>utc(clock()):raise ValueError('historical completed sessions required')
    output=Path(output).resolve()
    if Path(calendar).resolve()==output or Path(calendar).resolve() in output.parents:raise ValueError('output must be separate')
    output.mkdir(parents=True,exist_ok=False)
    reg={'schema':1,'scope':'historical_repair_not_original_PIT_or_prospective_data','start':start,'end':end,
         'days':requested,'calendar_manifest_id':identity(members),'started_at':utc(clock()).isoformat(),
         'origin':'xiaodefa_relay' if client is None else 'synthetic_fixture','max_requests':2*len(requested),'retries':0,
         'source_sha256':file_hash(Path(__file__)),
         'fallback_reason':'HiThink exposes action events rather than daily factors; no equivalent moneyflow endpoint in published map',
         'money_amount_unit':'CNY','source_money_amount_unit':'ten_thousand_CNY','execution_ready':False}
    write_json(output/'registration.json',reg);summaries=[]
    try:
        client=client or Relay()
        for day in requested:
            for api in FIELDS:
                data=client.query(api,day)
                normalized=normalize(api,day,data)
                rec={'api':api,'params':{'trade_date':day.replace('-','')},'data':data,'received_at':utc(clock()).isoformat()}
                write_json(output/f'{api}-{day}.json',rec)
                summaries.append({'api':api,'day':day,'rows':len(normalized),'response_id':identity(rec)})
        result={'registration_id':identity(reg),'receipts':summaries,'origin':reg['origin'],
                'historical_PIT_qualified':False,'research_ready':False,'execution_ready':False}
        write_json(output/'report.json',result);seal(output)
        return result
    except Exception as exc:
        write_json(output/'failed.json',{'error_type':type(exc).__name__,'receipts_retained':len(summaries),'execution_ready':False})
        raise


def verify(folder,*,clock=now_utc):
    members=sealed(folder);folder=Path(folder)
    if 'collection.json' in members:
        from .research_history import verify_collection
        return verify_collection(folder,clock=clock)
    reg=read_json(folder/'registration.json')[0]
    if (reg['schema']!=1 or reg['scope']!='historical_repair_not_original_PIT_or_prospective_data'
        or reg['origin'] not in ('xiaodefa_relay','synthetic_fixture') or reg['money_amount_unit']!='CNY'
        or reg['source_money_amount_unit']!='ten_thousand_CNY' or reg['execution_ready'] is not False):
        raise ValueError('repair origin and unit contract invalid')
    days=reg['days']
    if not 3<=len(days)<=10 or days!=sorted(set(days)):raise ValueError('bounded unique sessions required')
    if reg['start']!=days[0] or reg['end']!=days[-3] or reg['max_requests']!=2*len(days) or reg['retries']!=0:
        raise ValueError('repair interval/request budget changed')
    required={'registration.json','report.json',*(f'{api}-{day}.json' for day in days for api in FIELDS)}
    if set(members)!=required:raise ValueError('exact repair request membership required')
    data={api:[] for api in FIELDS};summaries=[]
    for day in days:
        datetime.strptime(day,'%Y-%m-%d')
        for api in FIELDS:
            rec=read_json(folder/f'{api}-{day}.json')[0]
            if rec['api']!=api or rec['params']!={'trade_date':day.replace('-','')} or not utc(reg['started_at'])<=utc(rec['received_at'])<=utc(clock()):
                raise ValueError('receipt identity/time mismatch')
            if utc(day+'T16:00:00+08:00')>utc(rec['received_at']):raise ValueError('future factor observation refused')
            rows=normalize(api,day,rec['data']);data[api].extend(rows)
            summaries.append({'api':api,'day':day,'rows':len(rows),'response_id':identity(rec)})
    expected={'registration_id':identity(reg),'receipts':summaries,'origin':reg['origin'],
              'historical_PIT_qualified':False,'research_ready':False,'execution_ready':False}
    if read_json(folder/'report.json')[0]!=expected:raise ValueError('repair report derivation changed')
    return reg,data,{'manifest_id':identity(members),'receipts':summaries,'origin':reg['origin'],
                    'scope':reg['scope'],'money_amount_unit':'CNY','historical_PIT_qualified':False}


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('calendar','start','end','output'):p.add_argument('--'+key,required=True)
    a=p.parse_args();print(json.dumps(capture(a.calendar,a.start,a.end,a.output)))


if __name__=='__main__':main()


API_FIELDS={'daily':['ts_code','trade_date','open','high','low','close','vol','amount'],**FIELDS}


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
