"""Immutable current observations -> declared judgement -> next-session review.

This is a bounded limit-pool observation cohort, never a tradable signal or
whole-market ranking. Receipts are local timestamps, not authenticated humans.
"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain import canonical, identity, instrument, now_utc, number, utc
from .gap_evidence import read_json, write_json
from .rolling_research import file_hash

CST = ZoneInfo('Asia/Shanghai')
POOL = '/api/a-share/special-data/limit-up-pool'
CALENDAR = '/api/a-share/calendar/trading-days'
SCOPE = 'daily_limit_pool_observation_no_execution'
POLICY = {'version': 'limit-pool-watch-v1', 'max_candidates': 20,
          'order': 'observed_height_desc_then_instrument', 'max_pages': 5,
          'scope': 'official_limit_pool_not_all_market_selection'}
BLOCKING_SOURCE_GAPS = {'requested_session_absent_from_native_calendar',
    'not_current_post_close_snapshot','source_response_not_current_day',
    'source_response_older_than_two_hours','pool_response_before_close',
    'invalid_or_duplicate_source_rows_excluded'}


def local_day(moment):
    return utc(moment).astimezone(CST).date().isoformat()


def seal(folder):
    members = {p.name: file_hash(p) for p in folder.iterdir() if p.is_file()}
    write_json(folder/'completed.json', {'members': members, 'manifest_id': identity(members)})


def verify(folder):
    folder = Path(folder).resolve()
    manifest = read_json(folder/'completed.json')[0]
    paths = list(folder.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths):
        raise ValueError('flat immutable daily package required')
    members = {p.name: file_hash(p) for p in paths if p.name != 'completed.json'}
    if manifest != {'members': members, 'manifest_id': identity(members)}:
        raise ValueError('daily artifact bytes or membership changed')
    report = read_json(folder/'report.json')[0]
    validate_report(report)
    reg = read_json(folder/'registration.json')[0]
    responses = [read_json(p)[0] for p in sorted(folder.glob('response-*.json'))]
    if report != build_report(reg['trade_date'],responses,report['generated_at'],origin=reg['origin']):
        raise ValueError('daily report differs from retained native responses')
    return report


def validate_report(report):
    if report.get('report_id') != identity({k:v for k,v in report.items() if k != 'report_id'}):
        raise ValueError('daily report fingerprint changed')
    if report.get('scope') != SCOPE or report.get('execution_ready') is not False:
        raise ValueError('non-executable daily observation required')
    date.fromisoformat(report['trade_date'])
    utc(report['generated_at'])


def build_report(day, responses, created, *, origin):
    """Use exact date query + response timestamp, never claim row event-time proof."""
    date.fromisoformat(day)
    end = utc(created)
    if origin not in ('hithink_native', 'synthetic_fixture'):
        raise ValueError('explicit supported origin required')
    calendars = [r for r in responses if r['path'] == CALENDAR]
    pools = [r for r in responses if r['path'] == POOL]
    if len(calendars) != 1 or not pools or len(calendars)+len(pools) != len(responses):
        raise ValueError('one calendar and bounded explicit pool pages required')
    dates = [datetime.strptime(r['date'], '%Y%m%d').date().isoformat() for r in calendars[0]['data']['item']]
    if len(dates) != len(set(dates)) or dates != sorted(dates):
        raise ValueError('unique ordered provider trading dates required')
    gaps = ['row_event_time_not_proven', 'security_rules_and_catalyst_not_independently_verified',
            'real_account_not_imported', 'not_a_validated_stock_selection_strategy']
    if origin == 'synthetic_fixture':
        gaps.append('synthetic_fixture_not_real_market')
    if day not in dates:
        gaps.append('requested_session_absent_from_native_calendar')
    if local_day(end) != day or end.astimezone(CST).hour < 15:
        gaps.append('not_current_post_close_snapshot')
    rows = []
    pages = pools[0]['data']['pagination']['pages']
    total = pools[0]['data']['pagination']['total']
    if type(pages) is not int or not 1 <= pages <= POLICY['max_pages'] or len(pools) != pages:
        raise ValueError('incomplete or over-budget native pool pagination')
    if type(total) is not int or total < 0:
        raise ValueError('explicit nonnegative provider total required')
    for rec in responses:
        received = utc(rec['received_at'])
        stamp = datetime.fromtimestamp(float(number(rec['data']['timestamp']))/1000, tz=CST)
        if received > end or utc(stamp) > received + timedelta(seconds=5):
            raise ValueError('receipt or provider timestamp is in the future')
        if local_day(received) != day or stamp.date().isoformat() != day:
            gaps.append('source_response_not_current_day')
        if received - utc(stamp) > timedelta(hours=2):
            gaps.append('source_response_older_than_two_hours')
        if rec['path'] == POOL and stamp.hour < 15:
            gaps.append('pool_response_before_close')
    expected_ms = int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()*1000)
    for index, rec in enumerate(pools, 1):
        p = rec['data']['pagination']
        if p['page'] != index or p['pages'] != pages or p['total'] != total or rec['params'].get('page') != index:
            raise ValueError('native pagination identity changed')
        if rec['params'].get('date_ms') != expected_ms:
            raise ValueError('pool request date differs from frozen session')
        rows.extend(rec['data']['item'])
    if len(rows) != total or len(rows) > 1000:
        raise ValueError('provider pool total and retained row count differ')
    observations, rejected, seen = [], [], set()
    for index, row in enumerate(rows):
        try:
            code, exchange = row['thscode'].split('.')
            key = instrument(exchange+'.'+code)
            if row.get('ticker') != code:
                raise ValueError('ticker and exchange identity disagree')
            if key in seen:
                raise ValueError('duplicate pool identity')
            seen.add(key)
            close, pct = number(row['last_price']), number(row['price_change_ratio_pct'])
            height = row['continue_day_cnt']
            if close <= 0 or type(height) is not int or not 1 <= height <= 100:
                raise ValueError('invalid quote or observed height')
            name, reason = row.get('name'), row.get('limit_up_reason')
            if not isinstance(name, str) or not name.strip() or len(name)>200 or '\ufffd' in name:
                raise ValueError('missing or corrupt name')
            risks = ['sealed_limit_price_not_executable_quote', 'next_session_gap_and_nonfill_risk',
                     'daily_pool_cannot_establish_buy_or_sell_permission']
            if type(row.get('is_st')) is not bool or type(row.get('is_new')) is not bool:
                risks.append('special_status_unknown')
            if row.get('is_st') is True:
                risks.append('provider_reports_ST')
            if row.get('is_new') is True:
                risks.append('provider_reports_new_listing')
            if height >= 3:
                risks.append('multi_board_extension_risk')
            reason = reason if isinstance(reason, str) and '\ufffd' not in reason else ''
            item = {'instrument':key, 'name':name, 'close_cny':str(close),
                    'provider_change_pct':str(pct), 'observed_height':height,
                    'provider_reason':reason[:2000], 'reason_status':'provider_attribution_not_verified_catalyst',
                    'source_row_sha256':identity(row), 'risks':risks,
                    'support':['member_of_requested_day_official_limit_pool', 'observed_ladder_height'],
                    'state':'watch_only', 'execution_ready':False}
            observations.append({**item, 'candidate_id':identity([day, item])})
        except (KeyError, ValueError, TypeError) as exc:
            rejected.append({'row_index':index, 'row_sha256':identity(row), 'error_type':type(exc).__name__})
    if rejected:
        gaps.append('invalid_or_duplicate_source_rows_excluded')
    observations.sort(key=lambda r:(-r['observed_height'],r['instrument']))
    report = {'schema':1, 'scope':SCOPE, 'trade_date':day, 'generated_at':end.isoformat(),
        'origin':origin, 'policy':POLICY, 'calendar':dates, 'source_response_count':len(responses),
        'source_sha256':identity(responses), 'source_time_scope':'request_date_and_response_timestamp_not_row_event_time',
        'status':'source_check_required' if set(gaps) & (BLOCKING_SOURCE_GAPS | {
            'synthetic_fixture_not_real_market'}) else 'current_native_observation',
        'pool_total':total, 'valid_pool_rows':len(observations), 'rejected_rows':rejected,
        'candidates':observations[:POLICY['max_candidates']],
        'not_selected_instruments':[r['instrument'] for r in observations[POLICY['max_candidates']:]],
        'gaps':sorted(set(gaps)), 'execution_ready':False, 'signal_impact':'disabled',
        'actual_operator_return':None}
    return {**report, 'report_id':identity(report)}


def capture(day, folder, *, client=None, clock=now_utc):
    from .daily_session_view import render
    created = utc(clock())
    if local_day(created) != day:
        raise ValueError('native current capture only accepts the actual local date')
    origin = 'synthetic_fixture' if client is not None else 'hithink_native'
    if client is None:
        from trade_system.hithink_client import HiThinkClient
        client = HiThinkClient(timeout=15)
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder/'registration.json', {'trade_date':day, 'created_at':created.isoformat(),
        'policy':POLICY, 'origin':origin, 'retries':0, 'max_requests':6,
        'source_code_sha256':file_hash(Path(__file__)), 'credential_headers_retained':False})
    responses = []
    def request(path, params):
        data = client._get(path,params)
        record = {'path':path, 'params':params, 'received_at':utc(clock()).isoformat(), 'data':data}
        if len(canonical(record).encode()) > 8_000_000:
            raise ValueError('native decoded response exceeds byte budget')
        write_json(folder/f'response-{len(responses):02d}.json', record)
        responses.append(record)
        return data
    try:
        request(CALENDAR,{})
        ms = int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()*1000)
        params = {'date_ms':ms,'size':200,'sort_field':'limit_up_time','sort_dir':'asc'}
        data = request(POOL,{**params,'page':1})
        pages = data['pagination']['pages']
        if type(pages) is not int or not 1 <= pages <= POLICY['max_pages']:
            raise ValueError('pool pagination exceeds request budget')
        for page in range(2,pages+1):
            request(POOL,{**params,'page':page})
        report = build_report(day,responses,clock(),origin=origin)
        write_json(folder/'report.json',report)
        (folder/'index.html').write_text(render(report),encoding='utf-8')
        seal(folder)
        return report
    except Exception as exc:
        # Do not persist raw transport exception strings that might include credentials.
        write_json(folder/'failed.json',{'error_type':type(exc).__name__,
            'requests_retained':len(responses),'completed':False,'execution_ready':False})
        raise


NOTE_FIELDS = {'schema','scope','report_id','candidate_id','note_id','created_at',
               'operator','intent','hypothesis','invalidation'}


def check_note(note, report):
    validate_report(report)
    if not isinstance(note,dict) or set(note) != NOTE_FIELDS or note['schema'] != 1:
        raise ValueError('strict daily judgement fields required')
    if note['scope'] != 'daily_judgement_no_execution' or note['report_id'] != report['report_id']:
        raise ValueError('judgement must bind exact daily report')
    if note['candidate_id'] not in {c['candidate_id'] for c in report['candidates']}:
        raise ValueError('judgement candidate not in frozen report')
    if note['intent'] not in ('observe','reject','paper_hypothesis'):
        raise ValueError('non-executable judgement required')
    for key,limit in [('note_id',200),('operator',200),('hypothesis',4000),('invalidation',4000)]:
        if not isinstance(note[key],str) or not note[key].strip() or len(note[key]) > limit:
            raise ValueError('bounded explicit judgement fields required')
    if utc(note['created_at']) < utc(report['generated_at']):
        raise ValueError('judgement declaration predates frozen daily report')


def import_judgement(raw, report_folder, archive, *, clock=now_utc):
    if len(raw) > 32000:
        raise ValueError('judgement byte budget exceeded')
    report = verify(report_folder)
    note = json.loads(raw)
    check_note(note,report)
    archive = Path(archive).resolve()
    if Path(report_folder).resolve() == archive or Path(report_folder).resolve() in archive.parents:
        raise ValueError('judgements must not mutate sealed daily package')
    target = archive/(identity([note['report_id'],note['note_id']])+'.json')
    if target.exists():
        old = read_json(target)[0]
        if old['note'] != note or old.get('receipt_id') != identity({k:v for k,v in old.items() if k!='receipt_id'}):
            raise ValueError('judgement id conflict or receipt changed')
        return old
    received = utc(clock())
    if received < utc(note['created_at']) or received < utc(report['generated_at']):
        raise ValueError('local receipt precedes judgement declaration')
    record = {'note':note,'received_at':received.isoformat(),
        'identity_scope':'caller_declared_not_authenticated',
        'timing_scope':'prospectivity_decided_from_local_receipt_against_next_native_session',
        'execution_ready':False}
    record['receipt_id'] = identity(record)
    archive.mkdir(parents=True,exist_ok=True)
    write_json(target,record)
    return record


def read_judgements(archive, report, *, asof):
    if archive is None:
        return []
    paths = sorted(Path(archive).glob('*.json'))
    if len(paths) > 5000:
        raise ValueError('judgement archive budget exceeded')
    records = []
    for p in paths:
        r = read_json(p)[0]
        if r.get('note',{}).get('report_id') != report['report_id']:
            continue
        if r.get('receipt_id') != identity({k:v for k,v in r.items() if k!='receipt_id'}):
            raise ValueError('judgement receipt fingerprint changed')
        check_note(r['note'],report)
        if utc(r['received_at']) < utc(r['note']['created_at']) or utc(r['received_at']) > utc(asof):
            raise ValueError('invalid judgement receipt chronology')
        records.append(r)
    return records


def next_review(parent, following, notes, *, created, observations=None):
    validate_report(parent)
    validate_report(following)
    if (set(parent['gaps']) | set(following['gaps'])) & BLOCKING_SOURCE_GAPS:
        raise ValueError('source date, freshness or completeness must pass before next-session review')
    future = [d for d in following['calendar'] if d > parent['trade_date']]
    if not future or following['trade_date'] != min(future):
        raise ValueError('follow-up must be the immediate next native trading session; no skipping missing days')
    if following['status'] not in ('current_native_observation','source_check_required'):
        raise ValueError('invalid following source status')
    if utc(created) < utc(following['generated_at']) or utc(following['generated_at']) <= utc(parent['generated_at']):
        raise ValueError('follow-up chronology invalid')
    day = following['trade_date']
    deadline = utc(day+'T09:30:00+08:00')
    later = {c['instrument']:c for c in following['candidates']}
    # Top-N absence never implies absence from full source pool or a price loss.
    all_pool = set(later) | set(following['not_selected_instruments'])
    observations = observations or {}
    rows = []
    for n in notes:
        check_note(n['note'],parent)
        if n.get('receipt_id') != identity({k:v for k,v in n.items() if k!='receipt_id'}):
            raise ValueError('judgement receipt changed')
        if utc(n['received_at']) < utc(n['note']['created_at']) or utc(n['received_at']) > utc(created):
            raise ValueError('invalid judgement timing')
    for candidate in parent['candidates']:
        code = candidate['instrument']
        matched = [n for n in notes if n['note']['candidate_id']==candidate['candidate_id']]
        decisions = []
        for n in matched:
            check_note(n['note'],parent)
            if n.get('receipt_id') != identity({k:v for k,v in n.items() if k!='receipt_id'}):
                raise ValueError('judgement receipt changed')
            received = utc(n['received_at'])
            if received < utc(n['note']['created_at']) or received > utc(created):
                raise ValueError('invalid judgement timing')
            decisions.append({'note':n['note'],'receipt_id':n['receipt_id'],
                'timing':'recorded_before_next_open' if received<deadline else 'retrospective_not_prospective',
                'received_at':n['received_at']})
        bar = observations.get(code)
        if bar:
            if bar.get('date') != day or bar.get('source_status') != 'legacy_table_not_native_authenticated':
                raise ValueError('exact next-session observation provenance required')
            if any(number(bar[k])<=0 for k in ('open','high','low','close')):
                raise ValueError('invalid next-session prices')
        rows.append({'candidate_id':candidate['candidate_id'],'instrument':code,'name':candidate['name'],
            'next_date':day,'in_following_observed_pool':code in all_pool,
            'pool_absence_semantics':'not_in_observed_pool_is_not_suspension_or_loss',
            'observation':bar,'observation_status':'observed' if bar else 'daily_bar_missing_not_zero',
            'judgements':decisions,'judgement_status':'recorded' if decisions else 'no_human_judgement',
            'actual_operator_return':None,'execution_ready':False})
    body = {'schema':1,'scope':'next_session_observation_not_actual_return',
        'parent_report_id':parent['report_id'],'following_report_id':following['report_id'],
        'created_at':utc(created).isoformat(),'trade_date':day,'cohort_size':len(rows),'rows':rows,
        'execution_ready':False,'actual_operator_return':None,
        'source_gaps':sorted(set(parent['gaps']+following['gaps']))}
    return {**body,'review_id':identity(body)}


def legacy_observations(db, day, codes, *, asof=None):
    """Bounded READ ONLY optional prices; table labels are not native authentication."""
    import duckdb
    date.fromisoformat(day)
    asof = utc(asof or now_utc())
    if day > local_day(asof):
        raise ValueError('cannot query future next-session observations')
    if not codes or len(codes)>20 or len(codes)!=len(set(codes)):
        raise ValueError('bounded frozen cohort required')
    ts = [instrument(c).split('.')[1]+'.'+c.split('.')[0] for c in codes]
    placeholders = ','.join('?' for _ in ts)
    with duckdb.connect(str(Path(db).resolve(strict=True)),read_only=True) as con:
        con.execute('SET threads=1')
        raw = con.execute(f'''SELECT ts_code,date,open,high,low,close,change_pct,
            fetched_at,adjustment,provider FROM tushare_daily
            WHERE date=? AND ts_code IN ({placeholders}) ORDER BY ts_code''',[day,*ts]).fetchall()
    grouped = {}
    for code,d,o,h,l,c,pct,received,adjustment,provider in raw:
        short,exchange = code.split('.')
        grouped.setdefault(exchange+'.'+short,[]).append((d,o,h,l,c,pct,received,adjustment,provider))
    result = {}
    for code, values in grouped.items():
        if len(values)!=1:
            continue
        d,o,h,l,c,pct,received,adjustment,provider = values[0]
        if adjustment != 'none' or provider not in ('tushare','xiaodefa') or received is None:
            continue
        if not isinstance(received,datetime):
            continue
        interpreted = received.replace(tzinfo=CST) if received.tzinfo is None else received
        if utc(interpreted) > asof:
            continue
        try:
            prices = list(map(number,[o,h,l,c]))
            if min(prices)<=0 or prices[2]>min(prices[0],prices[3]) or prices[1]<max(prices[0],prices[3]):
                continue
            pct = str(number(pct))
        except ValueError:
            continue
        result[code] = {'date':str(d),'open':str(o),'high':str(h),'low':str(l),'close':str(c),
            'provider_change_pct':pct,'fetched_at_original':str(received),
            'provider_label':provider,'source_status':'legacy_table_not_native_authenticated',
            'receipt_time_scope':'legacy_naive_time_assumed_AsiaShanghai_if_naive',
            'metric_scope':'unadjusted_market_observation_not_realizable_T1_return',
            'source_row_sha256':identity([str(v) for v in values[0]])}
    return result
