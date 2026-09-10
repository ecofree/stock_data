"""Strict native HiThink/TuShare response mapping, no account or database writes."""
from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

from .domain import identity, instrument, number, utc
from .gap_evidence import validate_claim


VERSION = 'native-gap-v1'
HITHINK = 'https://fuyao.aicubes.cn'
RELAY = 'https://fastapic.stockai888.top'
PRICE_PATH = '/api/a-share/prices/historical'
ACTION_PATH = '/api/a-share/corporate-actions/adjustment-factors'
FIELDS = {
    'daily': 'ts_code,trade_date,open,high,low,close,vol,amount',
    'suspend_d': 'ts_code,trade_date,suspend_timing,suspend_type',
    'dividend': 'ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div_tax,record_date,ex_date,pay_date,div_listdate,imp_ann_date',
    'stock_basic': 'ts_code,list_status,list_date,delist_date'}


def ts_code(code):
    exchange, digits = instrument(code).split('.')
    return digits + '.' + exchange


def ms(day):
    return int(utc(day+'T00:00:00+08:00').timestamp()*1000)


def day_ms(value):
    if type(value) is not int:
        raise ValueError('integer millisecond date required')
    return datetime.fromtimestamp(value/1000, ZoneInfo('Asia/Shanghai')).date().isoformat()


def compact_day(value):
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise ValueError('native YYYYMMDD date required')
    return date.fromisoformat(value[:4]+'-'+value[4:6]+'-'+value[6:]).isoformat()


def request_spec(case, api):
    code, day = ts_code(case['instrument']), case['date']
    date.fromisoformat(day)
    if api == 'hithink_daily':
        request = {'family': 'hithink_official', 'api': api, 'method': 'GET', 'url': HITHINK+PRICE_PATH,
                   'params': {'thscode': code, 'interval': '1d', 'adjust': 'none', 'start': ms(day), 'end': ms(day)+86399999}}
    elif api == 'hithink_actions':
        request = {'family': 'hithink_official', 'api': api, 'method': 'GET', 'url': HITHINK+ACTION_PATH,
                   'params': {'thscode': code, 'from': day, 'to': day}}
    elif api in FIELDS:
        params = {'ts_code': code}
        params['ex_date' if api == 'dividend' else 'list_status' if api == 'stock_basic' else 'trade_date'] = 'D' if api == 'stock_basic' else day.replace('-', '')
        request = {'family': 'xiaodefa_tushare', 'api': api, 'method': 'POST', 'url': RELAY,
                   'params': params, 'fields': FIELDS[api]}
    else:
        raise ValueError('API not in the read-only allowlist')
    return {**request, 'case_id': case['case_id'], 'instrument': case['instrument'], 'date': day}


def build_requests(cases):
    if not 1 <= len(cases) <= 11 or len({c['case_id'] for c in cases}) != len(cases):
        raise ValueError('one to eleven unique audited cases required')
    requests = []
    for c in sorted(cases, key=lambda c: (c['date'], c['instrument'])):
        missing = 'missing_frozen_raw_bar' in c['reasons']
        for api in (['hithink_daily', 'daily', 'suspend_d', 'stock_basic'] if missing else ['hithink_actions', 'dividend']):
            requests.append(request_spec(c, api))
    if len(requests) > 28:
        raise ValueError('28-request total budget exceeded')
    # Every applicable official request precedes relay corroboration/fallback.
    return sorted(requests, key=lambda r: r['family'] != 'hithink_official')


def parse_json(raw):
    def unique(pairs):
        d = {}
        for key, value in pairs:
            if key in d:
                raise ValueError('duplicate native JSON field')
            d[key] = value
        return d
    p = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(p, dict) or type(p.get('code')) is not int:
        raise ValueError('explicit native business status required')
    return p


def convert_response(spec, raw):
    """Map exact request-bound native rows, retaining their row index/limitations.

    No absence claim, date substitution, return adjustment or inferred dividend
    from factor ratios. Bonus shares cannot be treated as immediately sellable
    splits; they remain unresolved alongside cash candidates.
    """
    case = {k: spec[k] for k in ('case_id', 'instrument', 'date')}
    if request_spec(case, spec['api']) != spec:
        raise ValueError('request differs from exact audited read-only contract')
    payload = parse_json(raw)
    if payload['code'] != 0:
        return {'status': 'provider_error', 'business_code': payload['code'], 'claims': [], 'native_rows': None, 'notes': []}
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ValueError('native success missing structured data')
    code, day, api = ts_code(spec['instrument']), spec['date'], spec['api']
    if spec['family'] == 'hithink_official':
        rows = data.get('item')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('HiThink item array required')
        if data.get('thscode', code) != code or data.get('ticker', code[:6]) != code[:6]:
            raise ValueError('native response identity mismatch')
        if api == 'hithink_actions' and data.get('thscode') != code:
            raise ValueError('corporate event exchange identity required')
        if api == 'hithink_daily' and data.get('adjust', 'none') != 'none':
            raise ValueError('adjusted native price is not raw evidence')
    else:
        names, items = data.get('fields'), data.get('items')
        if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names) or len(names) != len(set(names)):
            raise ValueError('unique native field array required')
        if not isinstance(items, list) or any(not isinstance(r, list) or len(r) != len(names) for r in items):
            raise ValueError('native row length mismatch; null padding prohibited')
        if not set(FIELDS[api].split(',')) <= set(names):
            raise ValueError('native requested columns missing')
        rows = [dict(zip(names, row)) for row in items]
    if len(rows) > 100:
        raise ValueError('exact-case row limit exceeded; no silent truncation')
    claims, notes = [], []
    seen = set()
    def add(kind, value, index):
        claim = {'instrument': spec['instrument'], 'date': day, 'kind': kind, 'value': value}
        validate_claim(claim)
        claims.append({'claim': claim, 'native_row_index': index})
    for i, r in enumerate(rows):
        if spec['family'] == 'xiaodefa_tushare' and r.get('ts_code') != code:
            raise ValueError('native row belongs to another instrument')
        if spec['family'] == 'hithink_official' and r.get('ticker', code[:6]) != code[:6]:
            raise ValueError('native row ticker mismatch')
        key = identity(r)
        if key in seen:
            notes.append({'row': i, 'reason': 'duplicate_native_row'})
            continue
        seen.add(key)
        if api in ('hithink_daily', 'daily'):
            row_day = day_ms(r['date_ms']) if api == 'hithink_daily' else compact_day(r['trade_date'])
            if row_day != day:
                raise ValueError('native bar outside requested exact date')
            prices = {k: str(number(r[k+'_price' if api == 'hithink_daily' else k])) for k in ('open', 'high', 'low', 'close')}
            if any(number(v) <= 0 for v in prices.values()):
                notes.append({'row': i, 'reason': 'zero_or_negative_price_not_market_evidence'})
                continue
            add('daily_bar', {**prices, 'currency': 'CNY', 'adjustment': 'none', 'finality': 'final'}, i)
        elif api == 'suspend_d':
            if compact_day(r['trade_date']) != day:
                raise ValueError('native session status outside requested date')
            state, timing = r['suspend_type'], r['suspend_timing']
            if state == 'S':
                add('session_status', {'status': 'suspended', 'coverage': 'full_session' if timing in (None, '') else 'intraday'}, i)
            else:
                notes.append({'row': i, 'reason': 'resumption_or_unknown_is_not_proof_of_actual_trade'})
        elif api == 'stock_basic':
            if r['list_status'] != 'D' or not r['delist_date']:
                notes.append({'row': i, 'reason': 'no_dated_delisting_evidence'})
                continue
            delisted = compact_day(r['delist_date'])
            if delisted <= day:
                add('lifecycle', {'status': 'delisted', 'effective_from': delisted, 'effective_to': day}, i)
            else:
                notes.append({'row': i, 'reason': 'future_delisting_does_not_explain_case'})
        else:
            event_day = day_ms(r['ex_date_ms']) if api == 'hithink_actions' else compact_day(r['ex_date'])
            if event_day != day:
                raise ValueError('native corporate action outside requested date')
            if api == 'dividend' and r['div_proc'] != '实施':
                notes.append({'row': i, 'reason': 'proposed_not_implemented'})
                continue
            cash = number(r['dividend_per_share'] if api == 'hithink_actions' else r['cash_div_tax'])
            bonus = number(r['per_share_bonus'] if api == 'hithink_actions' else r['stk_div'])
            if cash < 0 or bonus < 0:
                raise ValueError('negative corporate action value')
            if cash > 0:
                # Stable cross-source ID causes conflicting gross amounts to
                # remain a conflict instead of masquerading as two dividends.
                add('corporate_action', {'action_type': 'cash_dividend', 'action_id': 'cash:'+code+':'+day,
                                        'ex_date': day, 'gross_cny_per_share': format(cash.normalize(), 'f')}, i)
            if bonus > 0:
                notes.append({'row': i, 'reason': 'bonus_shares_require_registration_listing_and_entitlement_not_instant_split',
                              'per_share_bonus': str(bonus)})
    return {'status': 'mapped' if claims else 'empty_or_unresolved', 'business_code': 0,
            'native_rows': len(rows), 'claims': claims, 'notes': notes,
            'missing_fields': ['independent_origin_authentication', 'actual_receipt_at_historical_decision',
                               'corporate_action_entitlement_and_tax_settlement']}
