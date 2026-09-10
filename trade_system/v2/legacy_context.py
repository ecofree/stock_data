"""Read-only, bounded adapter from a frozen legacy database into research.

No current-source license or historical receipt guarantee is inferred from
table existence. Original rows are preserved in the caller's source backup;
the normalized envelope binds the whole source file hash supplied by caller.
"""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import duckdb

from .domain import instrument


def _stamp(value):
    if not isinstance(value, datetime):
        raise ValueError('legacy timestamp required')
    return value.replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat()


def _code(ts_code):
    code, exchange = ts_code.split('.')
    return instrument(exchange+'.'+code)


def load_snapshot(path, trade_date, *, source_sha256, imported_at=None):
    if len(source_sha256) != 64 or any(c not in '0123456789abcdef' for c in source_sha256.lower()):
        raise ValueError('verified immutable source SHA-256 required')
    imported_at = imported_at or datetime.now(timezone.utc).isoformat()
    products = {
        'daily_tushare':dict(delivery_provider='legacy_tushare', origin_family='tushare', source_api='daily',
                     metric_definition='unadjusted_daily_bar',unit='CNY',priority=2,license_status='unverified'),
        'daily_xiaodefa':dict(delivery_provider='xiaodefa', origin_family='tushare', source_api='daily',
                     metric_definition='unadjusted_daily_bar',unit='CNY',priority=1,license_status='unverified'),
        'members':dict(delivery_provider='hithink_index_api',origin_family='ths',source_api='legacy_ths_concept_stock_history',
                       metric_definition='dated_membership_not_catalyst',unit='membership',priority=0,license_status='unverified'),
        'limits':dict(delivery_provider='hithink',origin_family='ths',source_api='legacy_official_limit_pool',
                      metric_definition='observed_limit_up_pool',unit='count',priority=0,license_status='unverified'),
        'calendar':dict(delivery_provider='legacy_tushare',origin_family='tushare',source_api='trade_cal',
                        metric_definition='open_date_and_pretrade_date',unit='boolean',priority=1,license_status='unverified'),
    }
    bundle = {'schema_version':1,'mode':'historical_research','trade_date':trade_date,
              'asof':trade_date+'T23:59:59+08:00','source_sha256':source_sha256,
              'source_path':str(path),'products':products,'bars':[],'memberships':[],'limits':[],'calendar':[],
              'universe_certified':False,'calendar_authority_verified':False,'limit_pool_complete':False,
              'gaps':['legacy_naive_timestamps_assumed_AsiaShanghai','no_verified_historical_first_receipt',
                      'delisting_lifecycle_not_available','snapshot_membership_not_catalyst',
                      'no_intraday_auction_or_funds_evidence'], 'imported_at':imported_at}
    with duckdb.connect(str(path), read_only=True) as con:
        con.execute("SET threads=2")
        con.execute("SET memory_limit='512MB'")
        day = datetime.strptime(trade_date, '%Y-%m-%d').date()
        calendar = con.execute('''SELECT cal_date,is_open,pretrade_date,fetched_at FROM tushare_trade_cal
            WHERE exchange='SSE' AND cal_date<=? AND cal_date>=CAST(? AS DATE)-INTERVAL 40 DAY
            ORDER BY cal_date''',[day,day]).fetchall()
        current = [r for r in calendar if r[0] == day and r[1]]
        previous = current[-1][2] if current else None
        start = previous or day
        for d,opened,prev,received in calendar:
            bundle['calendar'].append({'date':d.isoformat(),'exchange':'SSE','is_open':opened,
                'previous_open':prev.isoformat() if prev else None,'product':'calendar','revision':str(received),
                'event_at':min(datetime.combine(d,time()),received).replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat(),
                'received_at':_stamp(received),'known_at':imported_at})
        master = con.execute('SELECT ts_code,stock_code,list_date FROM tushare_stock_basic').fetchall()
        mapping, expected = {}, set()
        def add_identity(ts_code, short):
            resolved = _code(ts_code)
            if short in mapping and mapping[short] != resolved:
                raise ValueError('ambiguous exchange identity in source; explicit resolution required')
            mapping[short] = resolved
            return resolved
        for ts_code,short,listed in master:
            resolved = add_identity(ts_code,short)
            if listed is not None and listed <= day:
                expected.add(resolved)
        prices = con.execute('''SELECT ts_code,stock_code,date,close,change_pct,turnover,fetched_at,amount_unit,adjustment,provider
            FROM tushare_daily WHERE date BETWEEN ? AND ? ORDER BY date,ts_code''',[start,day]).fetchall()
        if len(prices) > 30000:
            raise ValueError('bounded two-session adapter row budget exceeded')
        excluded = {'unmapped_members':0,'invalid_bar_contract':0,'corrupt_theme_name_rows':0}
        for ts_code,short,d,close,pct,amount,received,unit,adjustment,provider in prices:
            resolved = add_identity(ts_code,short)
            expected.add(resolved)
            if unit != 'thousand_yuan' or adjustment != 'none' or provider not in ('tushare','xiaodefa'):
                excluded['invalid_bar_contract'] += 1
                continue
            bundle['bars'].append({'date':d.isoformat(),'instrument':resolved,'close':close,
                'change_pct':pct,'amount_cny':float(amount)*1000 if amount is not None else None,
                'product':'daily_'+provider,'revision':str(received),'event_at':d.isoformat()+'T16:00:00+08:00',
                'received_at':_stamp(received),'known_at':imported_at})
        members = con.execute('''SELECT trade_date,concept_code,concept_name,stock_code,fetched_at,date_verified
            FROM ths_concept_stock_history WHERE trade_date BETWEEN ? AND ? AND source='hithink_index_api'
            ORDER BY trade_date,concept_code,stock_code,fetched_at''',[start,day]).fetchall()
        if len(members) > 250000:
            raise ValueError('bounded membership row budget exceeded')
        for d,theme,name,short,received,verified in members:
            if short not in mapping:
                excluded['unmapped_members'] += 1
                continue
            if '\ufffd' in str(name):
                excluded['corrupt_theme_name_rows'] += 1
            bundle['memberships'].append({'date':d.isoformat(),'theme_id':theme,'theme_name':name,
                'instrument':mapping[short],'date_verified':verified,'product':'members','revision':str(received),
                'event_at':d.isoformat()+'T00:00:00+08:00','received_at':_stamp(received),'known_at':imported_at})
        pool = con.execute('''SELECT trade_date,stock_code,continue_day_cnt,fetched_at FROM official_limit_pool
            WHERE trade_date BETWEEN ? AND ? AND source='hithink' ORDER BY trade_date,stock_code''',[start,day]).fetchall()
        for d,short,height,received in pool:
            if short not in mapping:
                continue
            bundle['limits'].append({'date':d.isoformat(),'instrument':mapping[short],'height':height,
                'product':'limits','revision':str(received),'event_at':d.isoformat()+'T15:00:00+08:00',
                'received_at':_stamp(received),'known_at':imported_at})
        bundle['expected_universe'] = sorted(expected)
        bundle['adapter_diagnostics'] = excluded
        bundle['universe_basis'] = 'dated_listing_snapshot_union_two_session_bars_not_complete_PIT_master'
    return bundle
