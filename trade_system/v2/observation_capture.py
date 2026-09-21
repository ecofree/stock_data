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


def register_sampling(output, session, controls=(), *, clock=now_utc):
    """Seal the entire pre-session scope, including rejected and missing objects."""
    from datetime import timedelta
    from .domain import utc
    from .journal_index import between
    from .operator_workflow import configured_account_risk
    from .research_product import write_pointer
    from trade_system.file_lock import FileLock
    output=Path(output);at=utc(clock());cutoff=utc(session+'T09:15:00+08:00')
    if not at<cutoff or (cutoff-at)>timedelta(days=7):raise ValueError('sampling must be registered before session 09:15, within seven days')
    config=read_json(output/'workspace-config.json')[0]
    if config.get('read_only') is not True or not config.get('market_database'):
        raise ValueError('read-only market calendar required for sampling')
    import duckdb
    with duckdb.connect(str(Path(config['market_database']).resolve(strict=True)),read_only=True) as con:
        flags=con.execute("SELECT exchange,is_open FROM tushare_trade_cal WHERE cal_date=? "
            "AND exchange IN ('SSE','SZSE') ORDER BY exchange",[session]).fetchall()
    if flags!=[('SSE',1),('SZSE',1)]:raise ValueError('verified open session required for sampling')
    with FileLock(output/'judgement.guard'):
        pointer=output/'sampling-current.json'
        if pointer.exists():
            current=read_json(pointer)[0]
            retained=read_json(Path(current['folder'])/'sampling.json')[0]
            if retained.get('session')==session:
                verified=read_sampling(current['folder'],session)
                if verified['sampling_id']!=current['sampling_id']:raise ValueError('sampling pointer differs')
                from trade_system.quote_transport import canonical_codes
                if canonical_codes(controls)!=sorted({r['instrument'] for r in verified['rows'] if r['role']=='predeclared_control'}):
                    raise ValueError('same-session controls already sealed; live scope may change separately')
                return verified
        notes=between(output,'note','1970-01-01T00:00:00+00:00',at.isoformat())
        plans=between(output,'plan','1970-01-01T00:00:00+00:00',at.isoformat())
        risk=configured_account_risk(output,at.isoformat());rows=[]
        revised={n.get('supersedes') for n in notes}
        for n in notes:
            if n['note_id'] not in revised:
                rows.append({'instrument':n['instrument'],'role':n['intent'],'evidence_id':n['note_id'],'included_at':n['received_at']})
        for plan in plans:
            if utc(plan['valid_until'])>cutoff:
                rows.append({'instrument':plan['instrument'],'role':'plan','evidence_id':plan['plan_id'],'included_at':plan['received_at']})
        for pos in risk.get('positions',[]):
            if pos['quantity']>0:rows.append({'instrument':pos['instrument'].split('.')[-1],
                'role':'holding','evidence_id':risk['snapshot_id'],'included_at':at.isoformat()})
        from trade_system.quote_transport import canonical_codes
        for code in canonical_codes(controls):
            rows.append({'instrument':code,'role':'predeclared_control','evidence_id':None,'included_at':at.isoformat()})
        codes=canonical_codes([r['instrument'] for r in rows])
        if not codes:raise ValueError('no sampling subjects; provide existing attention or explicit controls')
        value={'schema':1,'scope':'pre_session_observation_scope_not_full_market_PIT','session':session,
            'registered_at':at.isoformat(),'cutoff':cutoff.isoformat(),'rows':rows,'codes':codes,
            'account_status':risk['status'],'control_count':len(controls),'execution_ready':False}
        value['sampling_id']=identity(value)
        folder=output/'sampling'/value['sampling_id'];folder.mkdir(parents=True,exist_ok=False)
        write_json(folder/'sampling.json',value);seal(folder)
        write_pointer(output,'sampling-current.json',{'folder':str(folder.resolve()),'sampling_id':value['sampling_id']})
        return value


def read_sampling(folder,session):
    members=sealed(folder)
    if set(members)!={'sampling.json'}:raise ValueError('sampling membership changed')
    value=read_json(Path(folder)/'sampling.json')[0]
    from .domain import utc
    if (value['sampling_id']!=identity({k:v for k,v in value.items() if k!='sampling_id'})
        or value['session']!=session or not utc(value['registered_at'])<utc(session+'T09:15:00+08:00')):
        raise ValueError('sampling identity/session/cutoff differs')
    return value
