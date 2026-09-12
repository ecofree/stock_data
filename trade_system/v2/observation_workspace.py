"""Read-only quote projection; never infer realtime from receipt time alone."""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import math
import re

import duckdb

from .domain import identity
from trade_system.source_authority import SOURCE_POLICIES


MAX_CODES=200
TTL_SECONDS=300


def local_clock(value):
    clock=datetime.fromisoformat(value) if isinstance(value,str) else value
    if clock.tzinfo is None:raise ValueError('aware observation clock required')
    return clock.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)


def qualify(row,clock):
    if row['provider'] not in SOURCE_POLICIES['observation_quote'].provider_order:return 'unqualified_provider'
    if row.get('is_stale'):return 'source_marked_stale'
    price=row.get('price')
    if price is None or not math.isfinite(price) or price<=0:return 'invalid_price'
    event=row.get('source_event_time');received=row.get('fetched_at')
    if not event or not received:return 'missing_source_time'
    if event>received or received>clock:return 'invalid_time_order'
    if str(row['source_date'])!=str(event.date()):return 'source_date_mismatch'
    if event.date()!=clock.date() or (clock-event).total_seconds()>TTL_SECONDS:return 'expired'
    return 'current_observation_not_executable'


def retained_rows(con,codes,as_of):
    codes=sorted(set(codes))
    if len(codes)>MAX_CODES or any(not re.fullmatch('[0-9]{6}',c) for c in codes):
        raise ValueError('bounded six-digit observation universe required')
    clock=local_clock(as_of);rows=[]
    tables={r[0] for r in con.execute('SHOW TABLES').fetchall()}
    if codes and 'multi_source_quote' in tables:
        columns={r[0] for r in con.execute('DESCRIBE multi_source_quote').fetchall()}
        event='source_event_time' if 'source_event_time' in columns else 'NULL::TIMESTAMP AS source_event_time'
        placeholders=','.join('?' for _ in codes)
        result=con.execute(f'''SELECT source_date,asset_code,price,change_pct,provider,fetched_at,is_stale,{event}
            FROM multi_source_quote WHERE asset_type='stock'
            AND regexp_replace(asset_code,'[.].*$','') IN ({placeholders})
            AND source_date<=? AND fetched_at<=?
            QUALIFY dense_rank() OVER (PARTITION BY asset_code,provider ORDER BY source_date DESC,fetched_at DESC)=1
            ORDER BY asset_code,provider LIMIT 5001''',[*codes,clock.date(),clock])
        names=[c[0] for c in result.description]
        rows=[dict(zip(names,r)) for r in result.fetchall()]
    if codes and 'executable_quote_snapshot' in tables:
        placeholders=','.join('?' for _ in codes)
        result=con.execute(f'''SELECT trade_date AS source_date,stock_code AS asset_code,price,change_pct,
            provider,fetched_at,FALSE AS is_stale,
            coalesce(try_strptime(quote_time,'%Y%m%d%H%M%S'),
                     try_strptime(quote_time,'%Y-%m-%d %H:%M:%S')) AS source_event_time
            FROM executable_quote_snapshot WHERE stock_code IN ({placeholders})
            AND trade_date<=? AND fetched_at<=?
            QUALIFY dense_rank() OVER (PARTITION BY stock_code,provider ORDER BY trade_date DESC,fetched_at DESC)=1
            ORDER BY stock_code,provider LIMIT 5001''',[*codes,clock.date(),clock])
        names=[c[0] for c in result.description]
        rows.extend(dict(zip(names,r)) for r in result.fetchall())
    if len(rows)>5000:raise ValueError('quote observation budget exceeded')
    return rows


def project_rows(rows,codes,as_of):
    codes=sorted(set(codes));clock=local_clock(as_of)
    if len(codes)>MAX_CODES or any(not re.fullmatch('[0-9]{6}',c) for c in codes):
        raise ValueError('bounded six-digit observation universe required')
    if len(rows)>5000:raise ValueError('quote observation budget exceeded')
    order=SOURCE_POLICIES['observation_quote'].provider_order;projected=[]
    for code in codes:
        candidates=[r for r in rows if r['asset_code'].split('.')[0]==code]
        # One latest receipt per provider; old rows cannot mask a new bad response.
        latest={p:max(r['fetched_at'] for r in candidates if r['provider']==p and r['fetched_at'])
                for p in {r['provider'] for r in candidates if r['fetched_at']}}
        candidates=[dict(r) for r in candidates if r['fetched_at']==latest.get(r['provider'])]
        for r in candidates:r['qualification']=qualify(r,clock)
        good=[r for r in candidates if r['qualification']=='current_observation_not_executable']
        if good:
            rank=min(order.index(r['provider']) for r in good)
            top=[r for r in good if order.index(r['provider'])==rank]
            if len({(r['price'],r['source_event_time']) for r in top})!=1:
                chosen=None;state='conflicting_same_priority_quotes'
            else:chosen=top[0];state=chosen['qualification']
        else:chosen=None;state='no_qualified_current_quote'
        projected.append({'instrument':code,'state':state,'price':chosen['price'] if chosen else None,
            'provider':chosen['provider'] if chosen else None,
            'source_event_time':chosen['source_event_time'].isoformat() if chosen else None,
            'valid_until':(chosen['source_event_time']+timedelta(seconds=TTL_SECONDS)).isoformat() if chosen else None,
            'rejected_reasons':sorted({r['qualification'] for r in candidates if r not in good}),
            'retained_rows':len(candidates)})
    payload={'scope':'retained_source_quote_observation_not_order_or_account_quote','as_of':as_of,
        'source_time_zone':'Asia/Shanghai','ttl_seconds':TTL_SECONDS,'rows':projected,
        'qualified':sum(r['price'] is not None for r in projected),'execution_ready':False}
    return dict(payload,snapshot_id=identity(payload))


def project(con,codes,as_of):
    return project_rows(retained_rows(con,codes,as_of),codes,as_of)


def load_rows(source,codes,as_of):
    with duckdb.connect(str(Path(source).resolve(strict=True)),read_only=True) as con:
        con.execute('BEGIN TRANSACTION')
        return retained_rows(con,codes,as_of)


def snapshot(source,codes,as_of,*,additional_rows=()):
    return project_rows([*load_rows(source,codes,as_of),*additional_rows],codes,as_of)


def present(value,as_of):
    """Expire a sealed observation on read; never extend TTL or write a snapshot."""
    from copy import deepcopy
    if value['snapshot_id']!=identity({k:v for k,v in value.items() if k!='snapshot_id'}):
        raise ValueError('observation snapshot changed')
    clock=local_clock(as_of);result=deepcopy(value)
    for row in result['rows']:
        if row['valid_until'] and clock>datetime.fromisoformat(row['valid_until']):
            row.update(price=None,state='expired_after_publication')
    result['qualified']=sum(r['price'] is not None for r in result['rows'])
    result['display_as_of']=as_of
    return result
