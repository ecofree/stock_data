"""Bounded native daily bars and current topic membership, never execution quotes."""
from datetime import datetime, timedelta
from pathlib import Path
import re

from .daily_session import CST, local_day, validate_report, seal
from .domain import canonical, file_hash, identity, instrument, now_utc, number, utc
from .gap_evidence import read_json, write_json

PRICES='/api/a-share/prices/historical'
MEMBERS='/api/a-share-index/constituents/ths-stock-list'


def derive(reg, responses):
    day=reg['trade_date']; date=datetime.fromisoformat(day).replace(tzinfo=CST)
    start=int(date.timestamp()*1000)
    if reg['origin'] not in ('hithink_native','synthetic_fixture'):
        raise ValueError('explicit capture origin required')
    codes=reg['instruments']; themes=reg['themes']
    if len(codes)>20 or len(set(codes))!=len(codes) or len(themes)>3 or len(set(themes))!=len(themes):
        raise ValueError('bounded unique cohort required')
    if len(responses)!=len(codes)+len(themes):
        raise ValueError('incomplete native enrichment request set')
    prices,topics,missing={},{},[]
    for idx,rec in enumerate(responses):
        received=utc(rec['received_at'])
        if received<utc(reg['started_at']):
            raise ValueError('receipt precedes registration')
        if received < utc(day+'T15:00:00+08:00'):
            raise ValueError('daily bars cannot be reviewed before requested close')
        stamp=rec['data'].get('timestamp')
        if stamp is not None and datetime.fromtimestamp(float(number(stamp))/1000,tz=CST)>received+timedelta(seconds=5):
            raise ValueError('future source timestamp')
        rows=rec['data']['item']
        if not isinstance(rows,list):
            raise ValueError('native item array required')
        if idx<len(codes):
            code=instrument(codes[idx]); exchange,ticker=code.split('.')
            expected={'thscode':ticker+'.'+exchange,'interval':'1d','start':start,
                      'end':start+86400000-1,'adjust':'none','offset':0}
            if rec['path']!=PRICES or rec['params']!=expected or len(rows)>1:
                raise ValueError('exact singleton unadjusted daily request required')
            if not rows:
                missing.append(code); continue
            row=rows[0]
            if local_day(datetime.fromtimestamp(float(number(row['date_ms']))/1000,tz=CST))!=day:
                raise ValueError('provider returned another session')
            values={k:str(number(row[k+'_price'])) for k in ('open','high','low','close')}
            o,h,l,c=[number(values[k]) for k in ('open','high','low','close')]
            if min(o,h,l,c)<=0 or l>min(o,c) or h<max(o,c):
                raise ValueError('native OHLC bounds invalid')
            if number(row['volume'])<0 or number(row['turnover'])<0:
                raise ValueError('negative volume/turnover')
            prices[code]={'date':day,**values,'instrument':code,'adjustment':'none',
                'volume_shares':str(number(row['volume'])),'turnover_cny':str(number(row['turnover'])),
                'source_status':'native_daily_bar_receipt' if reg['origin']=='hithink_native' else 'synthetic_daily_bar_receipt',
                'received_at':rec['received_at'],'response_sha256':identity(rec),
                'request_identity':expected,'source_row_sha256':identity(row),
                'finality':'provider_daily_bar_not_independently_exchange_certified'}
        else:
            theme=themes[idx-len(codes)]
            if not re.fullmatch(r'\d{6}\.TI',theme) or rec['path']!=MEMBERS or rec['params']!={'thscode':theme} or len(rows)>6000:
                raise ValueError('bounded explicit topic membership required')
            members=[]
            for row in rows:
                ticker,exchange=row['thscode'].split('.')
                if row.get('ticker')!=ticker:
                    raise ValueError('topic member identity mismatch')
                members.append(instrument(exchange+'.'+ticker))
            if len(set(members))!=len(members):
                raise ValueError('duplicate topic members')
            topics[theme]={'cohort_members':sorted(set(members)&set(codes)),
                'total_members':len(members),'response_sha256':identity(rec),'received_at':rec['received_at'],
                'scope':'current_membership_at_receipt_not_historical_constituents_or_catalyst_proof'}
    result={'scope':'native_enrichment_observation_only','parent_report_id':reg['parent_report_id'],
            'trade_date':day,'origin':reg['origin'],'observations':prices,'missing_instruments':missing,
            'topics':topics,'responses_sha256':identity(responses),'execution_ready':False}
    return {**result,'enrichment_id':identity(result)}


def capture(report, day, output, *, themes=(), client=None, clock=now_utc):
    validate_report(report)
    now=utc(clock())
    if utc(day+'T15:00:00+08:00')>now or day<report['trade_date']:
        raise ValueError('requested close not yet observable or precedes cohort')
    if len(themes)>3 or len(set(themes))!=len(themes) or any(not re.fullmatch(r'\d{6}\.TI',t) for t in themes):
        raise ValueError('at most three explicit unique THS themes')
    reg={'parent_report_id':report['report_id'],'trade_date':day,'started_at':now.isoformat(),
         'origin':'hithink_native' if client is None else 'synthetic_fixture',
         'instruments':[c['instrument'] for c in report['candidates']],'themes':list(themes),
         'source_code_sha256':file_hash(Path(__file__)),'max_requests':23,'retries':0}
    if len(reg['instruments'])>20 or len(set(reg['instruments']))!=len(reg['instruments']):
        raise ValueError('at most twenty unique frozen candidates')
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=False)
    write_json(output/'registration.json',reg)
    responses=[]
    start=int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()*1000)
    requests=[]
    for code in reg['instruments']:
        exchange,ticker=instrument(code).split('.')
        requests.append((PRICES,{'thscode':ticker+'.'+exchange,'interval':'1d',
            'start':start,'end':start+86400000-1,'adjust':'none','offset':0}))
    requests.extend((MEMBERS,{'thscode':t}) for t in themes)
    try:
        if client is None:
            from trade_system.hithink_client import HiThinkClient
            client=HiThinkClient(timeout=15,max_response_bytes=8_000_000,single_attempt=True)
        for path,params in requests:
            data=client._get(path,params)
            rec={'path':path,'params':params,'data':data,'received_at':utc(clock()).isoformat()}
            if len(canonical(rec).encode())>8_000_000:
                raise ValueError('decoded response byte budget exceeded')
            write_json(output/f'response-{len(responses):02d}.json',rec); responses.append(rec)
        result=derive(reg,responses); write_json(output/'report.json',result); seal(output)
        return result
    except Exception as exc:
        write_json(output/'failed.json',{'error_type':type(exc).__name__,'retained_responses':len(responses)})
        raise


def verify(folder):
    folder=Path(folder).resolve()
    paths=list(folder.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths):
        raise ValueError('flat immutable enrichment required')
    members={p.name:file_hash(p) for p in paths if p.name!='completed.json'}
    if read_json(folder/'completed.json')[0]!={'members':members,'manifest_id':identity(members)}:
        raise ValueError('native enrichment membership/bytes changed')
    result=derive(read_json(folder/'registration.json')[0],
        [read_json(p)[0] for p in sorted(folder.glob('response-*.json'))])
    if result!=read_json(folder/'report.json')[0]:
        raise ValueError('native enrichment derivation changed')
    return result
