"""Evidence-bound retrospective exclusions, never a tradability certificate."""
from collections import defaultdict
from datetime import date
from pathlib import Path
import re

from .domain import file_hash, identity, now_utc, utc, number
from .gap_evidence import read_json


def validate(path):
    path = Path(path).resolve(strict=True)
    value, _ = read_json(path)
    if (set(value) != {'schema','scope','received_at','sources','aliases','suspensions'}
        or value['schema'] != 1 or value['scope'] != 'retrospective_analyst_transcription_not_PIT_or_execution'
        or utc(value['received_at']) > now_utc()):
        raise ValueError('retrospective semantics contract invalid')
    sources = value['sources']
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 16:
        raise ValueError('bounded source references required')
    for source in sources.values():
        if set(source) != {'path','sha256'} or Path(source['path']).is_absolute():
            raise ValueError('relative source and digest required')
        target = path.parent/source['path']
        if target.is_symlink() or not target.is_file() or target.stat().st_size > 4_000_000:
            raise ValueError('bounded regular evidence source required')
        if file_hash(target) != source['sha256']:
            raise ValueError('semantics source changed')
    def code(v):
        if not isinstance(v,str) or not re.fullmatch(r'[0-9]{6}',v):
            raise ValueError('six digit source key required')
    aliases = value['aliases']; suspensions = value['suspensions']
    if not isinstance(aliases,list) or not isinstance(suspensions,list) or len(aliases)+len(suspensions)>128:
        raise ValueError('bounded semantic assertions required')
    used = set()
    for a in aliases:
        if set(a) != {'old','new','effective','source'} or a['source'] not in sources:
            raise ValueError('exact alias evidence required')
        code(a['old']); code(a['new']); date.fromisoformat(a['effective'])
        if a['old']==a['new'] or {a['old'],a['new']} & used:
            raise ValueError('alias collisions or chains require separate review')
        if a['effective'] > value['received_at'][:10]: raise ValueError('future alias refused')
        used.update((a['old'],a['new']))
    intervals = {}
    for s in suspensions:
        if set(s) != {'code','start','resume','source'} or s['source'] not in sources:
            raise ValueError('exact suspension evidence required')
        code(s['code']); start=date.fromisoformat(s['start']); end=date.fromisoformat(s['resume'])
        if not start < end or s['resume'] > value['received_at'][:10]:
            raise ValueError('completed exclusive resumption boundary required; no placeholders')
        for left,right in intervals.setdefault(s['code'],[]):
            if start < right and left < end: raise ValueError('overlapping suspension assertions')
        intervals[s['code']].append((start,end))
    return value, {'manifest_id':identity(value),'source_hashes':{k:v['sha256'] for k,v in sources.items()},
                   'scope':value['scope'],'received_at':value['received_at'],
                   'historical_PIT_qualified':False,'research_ready':False,'execution_ready':False}


def apply(con, path):
    value, meta = validate(path)
    con.execute('CREATE TEMP TABLE semantic_aliases(old_code VARCHAR,new_code VARCHAR,effective DATE,source VARCHAR)')
    con.execute('CREATE TEMP TABLE semantic_suspensions(code VARCHAR,start_date DATE,resume_date DATE,source VARCHAR)')
    if value['aliases']:
        con.executemany('INSERT INTO semantic_aliases VALUES (?,?,?,?)',
                        [(a['old'],a['new'],a['effective'],a['source']) for a in value['aliases']])
    if value['suspensions']:
        con.executemany('INSERT INTO semantic_suspensions VALUES (?,?,?,?)',
                        [(s['code'],s['start'],s['resume'],s['source']) for s in value['suspensions']])
    # No remapping of joins, raw identifiers, prices or money. This is an
    # exclusion/annotation layer over the already materialized historical target.
    con.execute('''CREATE TEMP TABLE semantic_flags AS WITH calendar AS (
        SELECT cal_date,lead(cal_date) OVER (ORDER BY cal_date) AS t1,
          lead(cal_date,2) OVER (ORDER BY cal_date) AS t2
        FROM verified_calendar_overlay WHERE is_open
      ) SELECT f.*,a.old_code AS alias_hint,a.source AS identity_evidence,
        EXISTS(SELECT 1 FROM semantic_aliases x WHERE
          (f.instrument=x.new_code AND f.datetime<x.effective)
          OR (f.instrument=x.old_code AND c.t2>=x.effective)) AS identity_conflict,
        EXISTS(SELECT 1 FROM semantic_suspensions s WHERE s.code=f.instrument
          AND f.datetime>=s.start_date AND f.datetime<s.resume_date) AS suspended_today,
        EXISTS(SELECT 1 FROM semantic_suspensions s WHERE s.code=f.instrument
          AND c.t1>=s.start_date AND c.t1<s.resume_date) AS suspended_t1,
        EXISTS(SELECT 1 FROM semantic_suspensions s WHERE s.code=f.instrument
          AND c.t2>=s.start_date AND c.t2<s.resume_date) AS suspended_t2,
        (p.volume IS NULL OR NOT isfinite(p.volume) OR p.volume<=0
          OR n.volume IS NULL OR NOT isfinite(n.volume) OR n.volume<=0
          OR n2.volume IS NULL OR NOT isfinite(n2.volume) OR n2.volume<=0) AS missing_positive_volume
      FROM all_features f LEFT JOIN calendar c ON c.cal_date=f.datetime
      LEFT JOIN semantic_aliases a ON a.new_code=f.instrument AND f.datetime<a.effective
      LEFT JOIN tushare_daily p ON p.stock_code=f.instrument AND p.date=f.datetime
      LEFT JOIN tushare_daily n ON n.stock_code=f.instrument AND n.date=c.t1
      LEFT JOIN tushare_daily n2 ON n2.stock_code=f.instrument AND n2.date=c.t2''')
    con.execute('''CREATE TEMP TABLE semantic_features AS SELECT
        * EXCLUDE(label_next_ret,label_status,alias_hint),
        coalesce(alias_hint,instrument) AS historical_instrument_hint,
        label_next_ret AS historical_price_target_ret,
        CASE WHEN NOT(identity_conflict OR suspended_today OR suspended_t1 OR suspended_t2 OR missing_positive_volume)
             THEN label_next_ret END AS label_next_ret,
        CASE WHEN identity_conflict THEN 'identity_conflict_unverified_continuity'
             WHEN suspended_today OR suspended_t1 OR suspended_t2 THEN 'documented_suspension_in_target_path'
             WHEN missing_positive_volume THEN 'missing_positive_volume_not_inferred_suspension'
             WHEN label_next_ret IS NULL THEN 'missing_session_price_not_zero'
             ELSE 'historical_proxy_not_execution_certified' END AS label_status,
        CAST(NULL AS DOUBLE) AS execution_target_ret,FALSE AS execution_qualified
      FROM semantic_flags''')
    if con.execute('SELECT count(*) FROM semantic_features').fetchone()!=con.execute('SELECT count(*) FROM all_features').fetchone():
        raise ValueError('semantic joins changed cohort cardinality')
    counts=con.execute('''SELECT label_status,count(*) FROM semantic_features GROUP BY label_status ORDER BY label_status''').fetchall()
    return {**meta,'context_label_status_counts':dict(counts),
            'context_scope':'entire_receipt_interval_including_warmup_and_label_tail',
            'identity_policy':'hint_only_no_cross_code_data_merge',
            'suspension_policy':'start_inclusive_resume_exclusive_current_T1_T2',
            'unknown_policy':'absence_of_evidence_is_not_tradeability','source_database_modified':False}


OBSERVED_UNIT_POLICY = {'version': 'observed-price-units-v1',
    'scope': 'exact_observed_rows_only_retrospective_not_identity_authority',
    'scales': [1, 10, 100, 1000, 10000, 1000000],
    'ohlc_tolerance': '0.00000001', 'volume_tolerance': '0.000001',
    'amount_tolerance': '0.01', 'cross_source_amount_tolerance': '0.50',
    'relative_scale_tolerance': '0.000000001'}
OBSERVED_PRICE_FIELDS = ['ts_code', 'stock_code', 'date', 'open', 'high', 'low', 'close',
    'volume', 'turnover', 'volume_unit', 'amount_unit', 'adjustment', 'provider', 'fetched_at']


def scale_for(raw, observed, tolerance):
    if raw is None or observed is None:
        return None
    raw, observed = number(raw), number(observed)
    if raw <= 0 or observed <= 0:
        return None
    tolerance = max(number(tolerance), abs(observed)*number(OBSERVED_UNIT_POLICY['relative_scale_tolerance']))
    matches = [s for s in OBSERVED_UNIT_POLICY['scales'] if abs(raw*s-observed) <= tolerance]
    return matches[0] if len(matches) == 1 else None


def qualify_observed_price(original, evidence, *, exchange='SZ', amount_tolerance=None):
    """No alias inference: both providers must match the stored six-digit code."""
    if exchange not in ('SZ', 'SH'):
        raise ValueError('explicit supported exchange required')
    amount_tolerance = OBSERVED_UNIT_POLICY['amount_tolerance'] if amount_tolerance is None else amount_tolerance
    if not 0 < number(amount_tolerance) <= number(OBSERVED_UNIT_POLICY['cross_source_amount_tolerance']):
        raise ValueError('bounded explicit amount tolerance required')
    row = {'original': original, 'original_sha256': identity(original),
        'status': 'unit_unqualified', 'reason': 'native_and_relay_required',
        'volume_shares': None, 'turnover_cny': None,
        'volume_scale': None, 'amount_scale': None, 'evidence': evidence}
    native = [e for e in evidence if e['provider'] == 'hithink_native']
    relay = [e for e in evidence if e['provider'] == 'xiaodefa_relay']
    if original['ts_code'] not in (original['stock_code']+'.'+exchange, exchange+'.'+original['stock_code']):
        row['reason'] = 'stored_code_conflict'
        return row
    if not native or not relay:
        return row
    if original['adjustment'] != 'none':
        row['reason'] = 'unadjusted_source_required'
        return row
    for e in evidence:
        if e['request_code'] != original['stock_code']+'.'+exchange or e['date'] != original['date']:
            raise ValueError('exact code/date binding required; aliases forbidden')
        if any(original[k] is None or abs(number(e['values'][k])-number(original[k])) > number(OBSERVED_UNIT_POLICY['ohlc_tolerance'])
               for k in ('open', 'high', 'low', 'close')):
            row['reason'] = 'source_price_conflict'
            return row
    authority = native[0]['values']
    for e in evidence:
        for field, tolerance in [('volume_shares', OBSERVED_UNIT_POLICY['volume_tolerance']),
                                 ('turnover_cny', OBSERVED_UNIT_POLICY['cross_source_amount_tolerance'])]:
            # Native duplicates must agree exactly, not inherit relay rounding allowance.
            allowed = 0 if e['provider'] == 'hithink_native' else number(tolerance)
            if abs(number(e['values'][field])-number(authority[field])) > allowed:
                row['reason'] = 'source_unit_conflict'
                return row
    volume_scale = scale_for(original['volume'], authority['volume_shares'], OBSERVED_UNIT_POLICY['volume_tolerance'])
    amount_scale = scale_for(original['turnover'], authority['turnover_cny'], amount_tolerance)
    if volume_scale is None or amount_scale is None:
        row['reason'] = 'scale_unknown_or_ambiguous'
        return row
    row.update(status='observed_row_unit_qualified', reason='native_priority_relay_corroborated',
        volume_scale=volume_scale, amount_scale=amount_scale,
        volume_shares=str(number(original['volume'])*volume_scale),
        turnover_cny=str(number(original['turnover'])*amount_scale),
        native_volume_residual=str(number(original['volume'])*volume_scale-number(authority['volume_shares'])),
        native_amount_residual=str(number(original['turnover'])*amount_scale-number(authority['turnover_cny'])))
    return row



RECONCILED_PRICE_POLICY = {'version': 'native_reconciled_price_slice_v1',
    'authority': 'hithink_native_same_request_code_and_date',
    'legacy_selection': 'none_all_variants_retained_and_reconciled',
    'cross_variant_volume_tolerance': '0.000001', 'cross_variant_amount_tolerance_cny': '0.50',
    'conflict': 'quarantine_entire_security_day', 'scope': 'registered_observations_only_not_identity_merge',
    'price_adjustment': 'none', 'volume_unit': 'shares', 'amount_unit': 'CNY'}


def resolve_observed_prices(rows):
    """Never pick latest/first old row. The independent native quote is the new row."""
    grouped = defaultdict(list)
    for row in rows:
        key = (row['original']['stock_code'], row['original']['date'])
        grouped[key].append(row)
    records = []
    for (code, day), variants in sorted(grouped.items()):
        variants = sorted(variants, key=lambda r: r['original_sha256'])
        record = {'stock_code': code, 'date': day, 'status': 'quarantined',
            'reason': 'all_source_variants_must_qualify', 'values': None,
            'source_variants': variants, 'native_evidence': [], 'source_variant_count': len(variants),
            'identity_qualified': False, 'research_ready': False, 'execution_ready': False}
        if all(r['status'] == 'observed_row_unit_qualified' for r in variants):
            evidence = {}
            for row in variants:
                for e in row['evidence']:
                    if e['provider'] == 'hithink_native':
                        evidence[identity(e)] = e
            natives = [evidence[k] for k in sorted(evidence)]
            agree = all(max(number(r[field]) for r in variants)-min(number(r[field]) for r in variants) <= number(tolerance)
                for field, tolerance in [('volume_shares', RECONCILED_PRICE_POLICY['cross_variant_volume_tolerance']),
                                         ('turnover_cny', RECONCILED_PRICE_POLICY['cross_variant_amount_tolerance_cny'])])
            if not natives or any(e['values'] != natives[0]['values'] for e in natives):
                record['reason'] = 'native_authority_missing_or_conflicting'
            elif not agree:
                record['reason'] = 'normalized_source_variants_conflict'
            else:
                record.update(status='canonical_price_observation',
                    reason='all_variants_reconciled_to_native' if len(variants)>1 else 'single_variant_reconciled_to_native',
                    values=natives[0]['values'], native_evidence=natives)
        record['record_id'] = identity(record)
        records.append(record)
    return records
