"""Bounded native price-basis and historical-calendar receipts; never rewrite sources."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.daily_session import verify,CALENDAR,CST,seal
from trade_system.v2.domain import canonical,file_hash,identity,now_utc,number,utc
from trade_system.v2.gap_evidence import write_json
from trade_system.v2.native_enrichment import PRICES
from trade_system.v2.research_receipts import calendar_overlay, MISSING_2024  # noqa: F401

def run(report_path,output):
    report=verify(report_path);day=report['trade_date'];now=now_utc()
    if utc(day+'T15:00:00+08:00')>now:raise ValueError('closed parent session required')
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    source_hash=file_hash(Path(__file__))
    codes=[c['instrument'] for c in report['candidates'][:3]]
    registration={'scope':'native_basis_gap_probe_not_research_qualification','parent_report_id':report['report_id'],
                  'codes':codes,'day':day,'started_at':now.isoformat(),'max_requests':14,'retries':0,
                  'source_sha256':source_hash,'execution_ready':False}
    write_json(output/'registration.json',registration)
    from trade_system.hithink_client import HiThinkClient
    client=HiThinkClient(timeout=15,max_response_bytes=8_000_000,single_attempt=True)
    receipts=[];failures=[]
    def request(path,params):
        try:
            data=client._get(path,params)
            receipt={'provider':'hithink_native','path':path,'params':params,'data':data,'received_at':now_utc().isoformat()}
            if len(canonical(receipt).encode())>8_000_000:raise ValueError('response over budget')
            write_json(output/f'receipt-{len(receipts):02d}.json',receipt);receipts.append(receipt)
            return data
        except Exception as exc:
            failures.append({'path':path,'params':params,'error_type':type(exc).__name__})
            return None
    calendar=request(CALENDAR,{})
    start=int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()*1000)
    comparisons=[]
    for code in codes:
        exchange,ticker=code.split('.');bars={}
        for adjust in ('none','forward','backward'):
            data=request(PRICES,{'thscode':ticker+'.'+exchange,'interval':'1d','start':start,
                                 'end':start+86400000-1,'adjust':adjust,'offset':0})
            if data is not None:
                rows=data.get('item',[])
                if len(rows)==1 and datetime.fromtimestamp(float(number(rows[0]['date_ms']))/1000,tz=CST).date().isoformat()==day:
                    r=rows[0];values=[number(r[k+'_price']) for k in ('open','high','low','close')]
                    if min(values)>0 and values[2]<=min(values[0],values[3]) and values[1]>=max(values[0],values[3]):
                        bars[adjust]={k:str(number(r[k+'_price'])) for k in ('open','high','low','close')}
        actions=request('/api/a-share/corporate-actions/adjustment-factors',{'thscode':ticker+'.'+exchange,'from':'2024-01-01','to':day})
        comparisons.append({'instrument':code,'bars':bars,'basis_modes_observed':sorted(bars),
            'company_action_rows':len(actions.get('item',[])) if actions is not None else None,
            'daily_factor_qualified':False,'historical_PIT_qualified':False})
    overlay=None
    try:
        from trade_system.config import SETTINGS
        from trade_system.tushare_relay import TushareRelayClient
        token=SETTINGS.get('XIAODEFA_TOKEN') or SETTINGS.get('TUSHARE_XIAODEFA_TOKEN')
        if not token:raise ValueError('xiaodefa credential unavailable')
        relay=TushareRelayClient(token=token,url=SETTINGS.get('XIAODEFA_URL') or 'https://t.xiaodefa.top/',resolve='',timeout=15,retries=1)
        params={'exchange':'SSE','start_date':'20240101','end_date':'20241231'}
        rows=relay.query_rows('trade_cal',params,'exchange,cal_date,is_open,pretrade_date')
        receipt={'provider':'xiaodefa_relay','api':'trade_cal','params':params,'rows':rows,'received_at':now_utc().isoformat(),
                 'fallback_reason':'HiThink documented calendar window excludes 2024'}
        if len(canonical(receipt).encode())>1_000_000:raise ValueError('calendar over budget')
        write_json(output/'calendar-relay-receipt.json',receipt)
        overlay=calendar_overlay(rows);overlay['receipt_sha256']=identity(receipt)
        write_json(output/'calendar-overlay.json',overlay)
    except Exception as exc:
        failures.append({'api':'xiaodefa.trade_cal','error_type':type(exc).__name__})
    dates=[r['date'] for r in calendar.get('item',[])] if calendar else []
    result={**registration,'native_calendar_range':[min(dates),max(dates)] if dates else None,
            'comparisons':comparisons,'calendar_overlay_available':overlay is not None,
            'missing_2024_dates_confirmed_open':all(overlay['target_dates'].values()) if overlay else False,
            'failures':failures,'source_unchanged':source_hash==file_hash(Path(__file__)),
            'historical_source_modified':False,'qualified_factor_rows':0,
            'remaining':['overlay_not_applied_to_new_export','2024_adjustment_coverage',
                         'fund_factor_receipts_and_units','PIT_universe_and_lifecycle']}
    write_json(output/'result.json',result);seal(output)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(json.dumps(run(a.report,a.output)))
