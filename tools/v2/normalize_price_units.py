"""Evidence-bound, retrospective price-unit slice; never a production repair."""
import argparse
from collections import Counter
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2 import probe_identity_sources as probe
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import canonical, file_hash, identity, number
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_receipts import sealed

POLICY = {'version': 'observed-price-units-v1', 'stock_code': '302132',
    'scope': 'exact_observed_rows_only_retrospective_not_identity_authority',
    'scales': [1, 10, 100, 1000, 10000, 1000000],
    'ohlc_tolerance': '0.00000001', 'volume_tolerance': '0.000001',
    'amount_tolerance': '0.01', 'cross_source_amount_tolerance': '0.50',
    'relative_scale_tolerance': '0.000000001'}
FIELDS = ['ts_code', 'stock_code', 'date', 'open', 'high', 'low', 'close',
    'volume', 'turnover', 'volume_unit', 'amount_unit', 'adjustment', 'provider', 'fetched_at']


def scale_for(raw, observed, tolerance):
    if raw is None or observed is None:
        return None
    raw, observed = number(raw), number(observed)
    if raw <= 0 or observed <= 0:
        return None
    tolerance = max(number(tolerance), abs(observed)*number(POLICY['relative_scale_tolerance']))
    matches = [s for s in POLICY['scales'] if abs(raw*s-observed) <= tolerance]
    return matches[0] if len(matches) == 1 else None


def qualify(original, evidence, *, exchange='SZ', amount_tolerance=None):
    """No alias inference: both providers must match the stored six-digit code."""
    if exchange not in ('SZ', 'SH'):
        raise ValueError('explicit supported exchange required')
    amount_tolerance = POLICY['amount_tolerance'] if amount_tolerance is None else amount_tolerance
    if not 0 < number(amount_tolerance) <= number(POLICY['cross_source_amount_tolerance']):
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
        if any(original[k] is None or abs(number(e['values'][k])-number(original[k])) > number(POLICY['ohlc_tolerance'])
               for k in ('open', 'high', 'low', 'close')):
            row['reason'] = 'source_price_conflict'
            return row
    authority = native[0]['values']
    for e in evidence:
        for field, tolerance in [('volume_shares', POLICY['volume_tolerance']),
                                 ('turnover_cny', POLICY['cross_source_amount_tolerance'])]:
            # Native duplicates must agree exactly, not inherit relay rounding allowance.
            allowed = 0 if e['provider'] == 'hithink_native' else number(tolerance)
            if abs(number(e['values'][field])-number(authority[field])) > allowed:
                row['reason'] = 'source_unit_conflict'
                return row
    volume_scale = scale_for(original['volume'], authority['volume_shares'], POLICY['volume_tolerance'])
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


def payload(receipts, db):
    receipts, db = Path(receipts).resolve(strict=True), Path(db).resolve(strict=True)
    code_hashes = {p.name: file_hash(p) for p in [Path(__file__), Path(probe.__file__)]}
    members = sealed(receipts)
    if read_json(receipts/'registration.json')[0]['origin'] != 'native_and_relay':
        raise ValueError('synthetic receipts cannot qualify a native price layer')
    # Replay the raw receipt membership, request fields, timestamps and returned rows.
    with TemporaryDirectory(prefix='stock-price-unit-replay-') as scratch:
        probe.analyze(receipts, db, Path(scratch)/'analysis')
        analysis = read_json(Path(scratch)/'analysis/result.json')[0]
    evidence = {}
    for request in analysis['requests']:
        if request['code'] != POLICY['stock_code']+'.SZ' or request['status'] != 'observed':
            continue
        if request['provider'] != 'hithink_native' and request['api'] != 'daily':
            continue
        filename = f"receipt-{request['index']:02d}.json"
        receipt = read_json(receipts/filename)[0]
        for day, values in request['normalized_rows'].items():
            evidence.setdefault(day, []).append({'provider': request['provider'],
                'request_code': request['code'], 'date': day, 'values': values,
                'receipt_file': filename, 'receipt_sha256': members[filename],
                'received_at': receipt['received_at'],
                'identity_binding': 'request_only' if request['provider'] == 'hithink_native' else 'returned_code_echo'})
    rows, seen = [], set()
    with duckdb.connect(str(db), read_only=True) as con:
        con.execute("SET threads=2")
        con.execute("SET memory_limit='512MB'")
        total = con.execute('SELECT count(*) FROM tushare_daily').fetchone()[0]
        for start, end in probe.WINDOWS:
            records = con.execute('SELECT '+','.join(FIELDS)+' FROM tushare_daily WHERE stock_code=? AND date BETWEEN ? AND ? ORDER BY date',
                [POLICY['stock_code'], start, end]).fetchall()
            if len(records) >= 64:
                raise ValueError('bounded source window required')
            for values in records:
                original = {k: None if v is None else str(v) for k, v in zip(FIELDS, values)}
                key = (original['stock_code'], original['date'])
                if key in seen:
                    raise ValueError('duplicate source security-day')
                seen.add(key)
                rows.append(qualify(original, evidence.get(original['date'], [])))
    counts = dict(Counter(r['status'] for r in rows))
    qualified = counts.get('observed_row_unit_qualified', 0)
    if members != sealed(receipts) or analysis['database_sha256'] != file_hash(db):
        raise ValueError('inputs changed during normalization')
    if code_hashes != {p.name: file_hash(p) for p in [Path(__file__), Path(probe.__file__)]}:
        raise ValueError('normalizer source changed')
    return {'policy': POLICY, 'source_code_sha256': code_hashes,
        'database_sha256': analysis['database_sha256'], 'receipt_manifest_id': identity(members),
        'origin': 'native_and_relay', 'source_total_rows': total, 'scoped_rows': len(rows),
        'qualified_rows': qualified, 'unqualified_or_out_of_scope_rows': total-qualified,
        'status_counts': counts, 'rows': rows, 'normalized_units': {'volume': 'shares', 'turnover': 'CNY'},
        'source_unchanged': True, 'source_labels_trusted': False, 'global_units_qualified': False,
        'identity_qualified': False, 'merge_authorized': False, 'point_in_time_qualified': False,
        'research_ready': False, 'execution_ready': False, 'production_cutover': False}


def build(receipts, db, output):
    output = Path(output)
    if output.exists():
        raise ValueError('new output directory required')
    result = payload(receipts, db)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'normalized-prices.json', result)
    seal(output)
    return {k: result[k] for k in ('scoped_rows', 'qualified_rows', 'unqualified_or_out_of_scope_rows', 'research_ready')}


def verify(folder, receipts, db):
    folder = Path(folder)
    members = sealed(folder)
    if set(members) != {'normalized-prices.json'}:
        raise ValueError('exact normalized layer membership required')
    result = read_json(folder/'normalized-prices.json')[0]
    if result != payload(receipts, db) or members != sealed(folder):
        raise ValueError('normalized layer differs from raw receipt/source replay')
    return result


def apply(con, folder, receipts, db):
    """TEMP-only qualified-unit relation. Missing join => NULL, never raw fallback."""
    databases = [r[2] for r in con.execute('PRAGMA database_list').fetchall() if r[2]]
    if len(databases) != 1 or Path(databases[0]).resolve() != Path(db).resolve(strict=True):
        raise ValueError('consumer must use the exact bound source database')
    result = verify(folder, receipts, db)
    con.execute('CREATE TEMP TABLE verified_price_units(stock_code VARCHAR,date DATE,volume_shares DOUBLE,turnover_cny DOUBLE,original_sha256 VARCHAR,PRIMARY KEY(stock_code,date))')
    qualified = [r for r in result['rows'] if r['status'] == 'observed_row_unit_qualified']
    if qualified:
        con.executemany('INSERT INTO verified_price_units VALUES (?,?,?,?,?)', [
            (r['original']['stock_code'], r['original']['date'], float(r['volume_shares']),
             float(r['turnover_cny']), r['original_sha256']) for r in qualified])
    return {k: result[k] for k in ('database_sha256', 'qualified_rows', 'research_ready')}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['build', 'verify'])
    p.add_argument('--receipts', required=True)
    p.add_argument('--db', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    result = build(a.receipts, a.db, a.output) if a.action == 'build' else {
        'verified': bool(verify(a.output, a.receipts, a.db)), 'research_ready': False}
    print(canonical(result))
