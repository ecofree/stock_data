"""Explicit bounded fallback quote receipts; never writes the source database."""
from datetime import datetime
from pathlib import Path

from trade_system.quote_transport import batches,parse_parts,request_bytes
from .daily_session import seal
from .domain import now_utc,identity
from .gap_evidence import read_json,write_json
from .research_receipts import sealed
from .observation_workspace import local_clock

SCOPE='explicit_tencent_fallback_observation_not_execution'
REASON='No verified native/relay live quote adapter; qualifying retained higher-priority quotes are preserved.'


def capture(folder,codes,*,fetch=None):
    requests=batches(codes);folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    write_json(folder/'registration.json',{'scope':SCOPE,'batches':requests,'retries':0,
        'origin':'synthetic_fixture' if fetch else 'tencent_https','fallback_reason':REASON,
        'started_at':now_utc().isoformat(),'execution_ready':False})
    for i,batch in enumerate(requests):
        try:
            raw=(fetch or request_bytes)(batch)
            if not isinstance(raw,bytes) or len(raw)>1_000_000:raise ValueError('response byte budget exceeded')
            with (folder/f'raw-{i}.bin').open('xb') as stream:stream.write(raw)
            received=now_utc().isoformat()
            # Save the provider bytes even if subsequent parsing fails.
            write_json(folder/f'receipt-{i}.json',{'received_at':received})
            count=len(parse_parts(raw,batch))
            status={'state':'observed','rows':count}
        except Exception as exc:
            status={'state':'failed','error_type':type(exc).__name__}
        write_json(folder/f'status-{i}.json',status)
    seal(folder)
    return replay(folder)


def replay(folder):
    folder=Path(folder);members=sealed(folder);reg=read_json(folder/'registration.json')[0]
    plan=batches([c for batch in reg['batches'] for c in batch])
    if (reg['scope']!=SCOPE or plan!=reg['batches'] or reg['retries']!=0
        or reg['execution_ready'] is not False or reg['origin'] not in ('tencent_https','synthetic_fixture')):
        raise ValueError('quote receipt registration differs')
    required={'registration.json',*(f'status-{i}.json' for i in range(len(plan)))}
    allowed=required|{f'{kind}-{i}.{ext}' for i in range(len(plan)) for kind,ext in [('raw','bin'),('receipt','json')]}
    if not required<=set(members)<=allowed:raise ValueError('quote receipt membership differs')
    rows=[];failed=0
    for i,batch in enumerate(plan):
        status=read_json(folder/f'status-{i}.json')[0]
        if status['state']=='failed':failed+=1;continue
        if status['state']!='observed':raise ValueError('unknown quote response status')
        received=read_json(folder/f'receipt-{i}.json')[0]['received_at']
        if local_clock(received)<local_clock(reg['started_at']):raise ValueError('receipt predates registration')
        parts=parse_parts((folder/f'raw-{i}.bin').read_bytes(),batch)
        if status['rows']!=len(parts):raise ValueError('quote receipt row count differs')
        for code,fields in parts.items():
            try:event=datetime.strptime(fields[30],'%Y%m%d%H%M%S')
            except ValueError:event=None
            try:price=float(fields[3])
            except ValueError:price=None
            rows.append({'asset_code':code,'source_date':event.date() if event else None,
                'price':price,'change_pct':None,'provider':'tencent_spot_quote','is_stale':False,
                'source_event_time':event,'fetched_at':local_clock(received)})
    return {'rows':rows,'codes':[c for batch in plan for c in batch],'origin':reg['origin'],
        'requests':len(plan),'failures':failed,'manifest_id':identity(members),
        'folder':str(folder.resolve()),'fallback_reason':REASON}
