"""Content-bound, locally received gap evidence. Never account/market authority.

JSON response imports are assertions, not proof of provider authenticity. Even
supported classifications are research candidates; neither prices nor corporate
actions are applied to an account by this module.
"""
from datetime import date
import hashlib
import json
from pathlib import Path

from .domain import canonical, identity, instrument, money, now_utc, number, utc


SOURCE_ORDER = {'hithink_official': 0, 'xiaodefa_tushare': 1, 'other': 2}
KINDS = {'daily_bar', 'session_status', 'lifecycle', 'corporate_action', 'collector_status'}
MAX_BYTES = 4 * 1024 * 1024


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('evidence artifact exceeds bounded import size')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('evidence artifact exceeds bounded import size')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique), raw


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        stream.write(canonical(value))


def validate_claim(claim):
    if set(claim) != {'instrument', 'date', 'kind', 'value'}:
        raise ValueError('strict claim envelope required')
    instrument(claim['instrument'])
    date.fromisoformat(claim['date'])
    kind, value = claim['kind'], claim['value']
    if kind not in KINDS or not isinstance(value, dict):
        raise ValueError('explicit structured claim kind required')
    if kind == 'daily_bar':
        if value.get('adjustment') != 'none' or value.get('currency') != 'CNY' or value.get('finality') != 'final':
            raise ValueError('unadjusted CNY evidence required')
        prices = {key: money(value[key]) for key in ('open', 'high', 'low', 'close')}
        if not 0 < prices['low'] <= min(prices['open'], prices['close']) <= max(prices['open'], prices['close']) <= prices['high']:
            raise ValueError('inconsistent raw OHLC evidence')
    elif kind == 'session_status':
        if value.get('status') not in ('suspended', 'traded', 'unknown') or value.get('coverage') not in ('full_session', 'intraday'):
            raise ValueError('explicit session status and coverage required')
    elif kind == 'lifecycle':
        if value.get('status') not in ('delisted', 'not_yet_listed', 'listed'):
            raise ValueError('explicit lifecycle state required')
        if not date.fromisoformat(value['effective_from']) <= date.fromisoformat(claim['date']) <= date.fromisoformat(value['effective_to']):
            raise ValueError('lifecycle claim does not cover case date')
    elif kind == 'corporate_action':
        if value.get('action_type') not in ('cash_dividend', 'split') or value.get('ex_date') != claim['date']:
            raise ValueError('explicit dated supported corporate action required')
        if value['action_type'] == 'cash_dividend':
            if number(value['gross_cny_per_share']) <= 0:
                raise ValueError('positive declared dividend required')
        elif number(value['numerator']) <= 0 or number(value['denominator']) <= 0:
            raise ValueError('positive split ratio required')
        if not value.get('action_id'):
            raise ValueError('corporate action identity required')
    elif value.get('outcome') not in ('failed', 'successful_empty', 'partial', 'complete') or not value.get('request_id'):
        raise ValueError('explicit collector request outcome required')
    canonical(claim)


def validate_receipt(receipt, raw, claim):
    required = {'schema_version', 'source_family', 'source_api', 'origin_family', 'reference',
                'source_event_at', 'provider_received_at', 'license_status', 'row_index', 'raw_sha256', 'source_revision'}
    if set(receipt) != required or receipt['schema_version'] != 1:
        raise ValueError('strict evidence receipt schema required')
    if receipt['source_family'] not in SOURCE_ORDER or receipt['license_status'] not in ('unverified', 'declared_permitted', 'restricted'):
        raise ValueError('explicit source family and license declaration required')
    if any(not isinstance(receipt[k], str) or not receipt[k].strip() for k in ('source_api', 'origin_family', 'reference', 'source_revision')):
        raise ValueError('API, origin and source reference required')
    if receipt['raw_sha256'] != sha(raw):
        raise ValueError('raw response checksum mismatch')
    if utc(receipt['source_event_at']) > utc(receipt['provider_received_at']):
        raise ValueError('provider receipt precedes source event')
    if claim['kind'] == 'daily_bar' and utc(receipt['source_event_at']) < utc(claim['date']+'T15:00:00+08:00'):
        raise ValueError('final daily bar precedes declared session close boundary')
    validate_claim(claim)


def ingest_response(receipt_path, raw_path, output, *, clock=now_utc):
    """Import one exact row from a structured response; local receipt is not user-set."""
    receipt, _ = read_json(receipt_path)
    response, raw = read_json(raw_path)
    if set(response) != {'schema_version', 'rows'} or response['schema_version'] != 1 or not isinstance(response['rows'], list):
        raise ValueError('adapter response must contain schema_version and structured rows')
    index = receipt['row_index']
    if type(index) is not int or not 0 <= index < len(response['rows']):
        raise ValueError('explicit response row index required')
    claim = response['rows'][index]
    validate_receipt(receipt, raw, claim)
    received = utc(clock())
    if utc(receipt['provider_received_at']) > received:
        raise ValueError('future provider receipt cannot be imported')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    with (output/'response.json').open('xb') as stream:
        stream.write(raw)
    record = {'receipt': receipt, 'claim': claim, 'ingested_at': received.isoformat(),
              'available_at': received.isoformat(), 'source_authenticity': 'not_independently_verified',
              'execution_ready': False}
    record['evidence_id'] = identity(record)
    write_json(output/'evidence.json', record)
    return record


def read_evidence(folder):
    folder = Path(folder).resolve()
    if {p.name for p in folder.iterdir()} != {'evidence.json', 'response.json'}:
        raise ValueError('evidence package membership changed')
    record, _ = read_json(folder/'evidence.json')
    if not isinstance(record, dict) or set(record) != {'receipt', 'claim', 'ingested_at', 'available_at',
            'source_authenticity', 'execution_ready', 'evidence_id'}:
        raise ValueError('invalid archived evidence envelope')
    key = record.pop('evidence_id')
    if identity(record) != key:
        raise ValueError('evidence record checksum mismatch')
    response, raw = read_json(folder/'response.json')
    if not isinstance(response, dict) or set(response) != {'schema_version', 'rows'} or response['schema_version'] != 1 or not isinstance(response['rows'], list):
        raise ValueError('invalid archived response envelope')
    receipt = record['receipt']
    index = receipt['row_index']
    if type(index) is not int or not 0 <= index < len(response['rows']) or response['rows'][index] != record['claim']:
        raise ValueError('imported claim does not match archived response row')
    validate_receipt(receipt, raw, record['claim'])
    if utc(record['available_at']) != utc(record['ingested_at']) or utc(receipt['provider_received_at']) > utc(record['ingested_at']):
        raise ValueError('invalid local knowledge timestamp')
    if record['source_authenticity'] != 'not_independently_verified' or record['execution_ready'] is not False:
        raise ValueError('import cannot certify source or execution')
    record['evidence_id'] = key
    return record


def adjudicate(case, evidence, *, asof, mode):
    """Priority ranks candidate sources but NEVER suppresses a contradiction.

    Historical repair may use currently received historical claims; system replay
    must exclude those learned after its as-of, even if provider times are old.
    No classification supplies a valuation or releases a halted portfolio.
    """
    if mode not in ('historical_repair', 'system_replay'):
        raise ValueError('explicit research repair or knowledge-time replay required')
    cutoff = utc(asof)
    included, excluded, seen, response_rows = [], [], set(), set()
    for row in sorted(evidence, key=lambda r: (SOURCE_ORDER[r['receipt']['source_family']], r['evidence_id'])):
        eid = row['evidence_id']
        if identity({k: v for k, v in row.items() if k != 'evidence_id'}) != eid:
            raise ValueError('adjudication evidence checksum mismatch')
        validate_claim(row['claim'])
        if utc(row['available_at']) != utc(row['ingested_at']) or utc(row['receipt']['provider_received_at']) > utc(row['ingested_at']):
            raise ValueError('invalid evidence knowledge clock')
        if eid in seen:
            continue
        seen.add(eid)
        c, p = row['claim'], row['receipt']
        if (c['instrument'], c['date']) != (case['instrument'], case['date']):
            excluded.append({'evidence_id': eid, 'reason': 'other_case'})
            continue
        reason = ('not_yet_known' if utc(row['available_at']) > cutoff or utc(p['source_event_at']) > cutoff else
                  'license_restricted' if p['license_status'] == 'restricted' else None)
        if reason:
            excluded.append({'evidence_id': eid, 'reason': reason})
            continue
        key = (p['raw_sha256'], p['row_index'])
        if key in response_rows:
            excluded.append({'evidence_id': eid, 'reason': 'duplicate_response_row_not_independent'})
            continue
        response_rows.add(key)
        included.append(row)
    included.sort(key=lambda r: (SOURCE_ORDER[r['receipt']['source_family']], r['evidence_id']))
    claims = [r['claim'] for r in included]
    bars = [c['value'] for c in claims if c['kind'] == 'daily_bar']
    suspension = any(c['kind'] == 'session_status' and c['value']['status'] == 'suspended' and c['value']['coverage'] == 'full_session' for c in claims)
    traded = bool(bars) or any(c['kind'] == 'session_status' and c['value']['status'] == 'traded' for c in claims)
    outside = any(c['kind'] == 'lifecycle' and c['value']['status'] in ('delisted', 'not_yet_listed') for c in claims)
    listed = any(c['kind'] == 'lifecycle' and c['value']['status'] == 'listed' for c in claims)
    prices = {tuple(money(b[k]) for k in ('open', 'high', 'low', 'close')) for b in bars}
    actions = [c['value'] for c in claims if c['kind'] == 'corporate_action']
    action_ids = {}
    for action in actions:
        action_ids.setdefault(action['action_id'], set()).add(identity(action))
    conflicts = []
    if suspension and traded:
        conflicts.append('full_session_suspension_vs_traded')
    if outside and (traded or listed or suspension):
        conflicts.append('lifecycle_vs_active_session')
    if len(prices) > 1:
        conflicts.append('raw_price_disagreement')
    if any(len(v) > 1 for v in action_ids.values()):
        conflicts.append('corporate_action_revision_disagreement')
    snapshot_prices = set()
    for row in case.get('observations', {}).get('tushare_daily', {}).get('rows', []):
        try:
            if row['adjustment'] == 'none':
                snapshot_prices.add(tuple(money(row[k]) for k in ('open', 'high', 'low', 'close')))
        except (ValueError, KeyError, TypeError):
            continue  # invalid legacy observations are not positive market evidence
    if snapshot_prices and (suspension or outside):
        conflicts.append('snapshot_raw_bar_vs_no_session')
    if bars and snapshot_prices and prices != snapshot_prices:
        conflicts.append('candidate_vs_snapshot_raw_price')
    if conflicts:
        classification = 'conflicting_evidence'
    elif outside:
        classification = 'lifecycle_supported_not_authenticated'
    elif suspension:
        classification = 'suspension_supported_not_authenticated'
    elif bars:
        classification = 'raw_bar_candidate_not_authenticated'
    elif actions:
        classification = 'corporate_action_candidate_not_authenticated'
    elif any(c['kind'] == 'collector_status' and c['value']['outcome'] in ('failed', 'partial') for c in claims):
        classification = 'collection_failure_supported_cause_unresolved'
    else:
        classification = 'unknown'
    # A confirmed suspension still cannot authorize carry-forward valuation;
    # stale marks, resumption and shareholder entitlements need separate rules.
    return {'case_id': case['case_id'], 'classification': classification, 'asof': cutoff.isoformat(), 'mode': mode,
            'included_evidence_ids': [r['evidence_id'] for r in included], 'excluded_evidence': excluded,
            'declared_origin_families': sorted({r['receipt']['origin_family'] for r in included}),
            'independently_verified_origins': 0, 'conflicts': conflicts,
            'preferred_evidence_id': included[0]['evidence_id'] if included and not conflicts else None,
            'candidate_raw_bar': bars[0] if bars and not conflicts and not outside and not suspension else None,
            'candidate_corporate_actions': [value for _, value in sorted({identity(a): a for a in actions}.items())] if not conflicts else [],
            'source_authenticity': 'not_independently_verified', 'can_apply_to_account': False,
            'can_resume_portfolio': False, 'can_forward_fill_mark': False,
            'execution_ready': False, 'signal_impact': 'disabled',
            'required_next_evidence': ['independently verify exact source response and revision',
                'verify exchange session/lifecycle and raw price/factor consistency',
                'corporate action requires entitlement, share/cost/tax reconciliation',
                'new immutable experiment and explicit valuation/exit policy before any resumption']}
