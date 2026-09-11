"""Fixed-budget 2025 batch/duplicate-key probe, not canonical price publication."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2 import normalize_price_units as units
from tools.v2 import probe_identity_sources as probe
from trade_system.v2.daily_session import CST, seal
from trade_system.v2.domain import canonical, file_hash, identity, now_utc, number, utc
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_receipts import sealed

SCOPE = 'retrospective_2025_batch_units_and_duplicate_keys_not_merge_authority'
SAMPLES = [(code, start, end) for code in ('002767.SZ', '000001.SZ', '600276.SH')
           for start, end in [('2025-01-02', '2025-01-10'), ('2025-12-25', '2025-12-31')]] + [
           ('600278.SH', '2025-07-28', '2025-08-05')]
POLICY = {'scope': SCOPE, 'version': 1, 'max_requests': 14, 'retries': 0,
    'amount_fit_tolerance_cny': '0.50', 'duplicate_resolution': 'forbidden',
    'sampling': 'four_fetched_date_groups_two_unit_regimes_and_midnight_boundary_not_random'}


def plan():
    requests = []
    for provider in ('hithink_native', 'xiaodefa_relay'):
        for code, start, end in SAMPLES:
            stamp = lambda d: int(datetime.fromisoformat(d).replace(tzinfo=CST).timestamp()*1000)
            params = {'thscode': code, 'interval': '1d', 'start': stamp(start),
                'end': stamp(end)+86400000-1, 'adjust': 'none', 'offset': 0} if provider == 'hithink_native' else {
                'ts_code': code, 'start_date': start.replace('-', ''), 'end_date': end.replace('-', '')}
            requests.append({'provider': provider, 'api': probe.PRICES if provider == 'hithink_native' else 'daily',
                'code': code, 'start': start, 'end': end, 'params': params})
    return requests


def source_hashes():
    paths = [Path(__file__), Path(units.__file__), Path(probe.__file__)]
    return {p.name: file_hash(p) for p in paths}


def capture(db, output, *, client=None):
    """No database writes. Declared origin is not a cryptographic provider signature."""
    output = Path(output)
    if output.exists():
        raise ValueError('new output directory required')
    db_hash = file_hash(db)
    output.mkdir(parents=True, exist_ok=False)
    registration = {'policy': POLICY, 'requests': plan(), 'database_sha256': db_hash,
        'received_after': now_utc().isoformat(), 'source_sha256': source_hashes(),
        'origin': 'native_and_relay' if client is None else 'synthetic_fixture',
        'execution_ready': False}
    write_json(output/'registration.json', registration)
    client = client or probe.Client()
    for index, request in enumerate(plan()):
        try:
            data = client.query(request)
            if len(canonical(data).encode()) > 3_900_000:
                raise ValueError('bounded receipt required')
            write_json(output/f'receipt-{index:02d}.json', {
                'request': request, 'received_at': now_utc().isoformat(), 'data': data})
            parsed = probe.rows_for(request, data)
            status = {'index': index, 'status': 'observed', 'rows': len(parsed)}
        except Exception as exc:
            status = {'index': index, 'status': 'failed', 'error_type': type(exc).__name__}
        write_json(output/f'status-{index:02d}.json', status)
        print(canonical(status), flush=True)
    if db_hash != file_hash(db):
        raise ValueError('source database changed; package left unsealed')
    seal(output)


def replay(folder):
    folder = Path(folder)
    members = sealed(folder)
    reg = read_json(folder/'registration.json')[0]
    if (reg['policy'] != POLICY or reg['requests'] != plan() or reg['execution_ready'] is not False
            or reg['origin'] not in ('native_and_relay', 'synthetic_fixture')
            or utc(reg['received_after']) > now_utc()):
        raise ValueError('fixed request registration changed')
    required = {'registration.json', *(f'status-{i:02d}.json' for i in range(14))}
    optional = {f'receipt-{i:02d}.json' for i in range(14)}
    if not required <= set(members) or not set(members) <= required | optional:
        raise ValueError('request package membership differs')
    evidence, statuses, returns = defaultdict(list), [], []
    for index, request in enumerate(plan()):
        status = read_json(folder/f'status-{index:02d}.json')[0]
        if status.get('index') != index or status.get('status') not in ('observed', 'failed'):
            raise ValueError('invalid request status')
        filename = f'receipt-{index:02d}.json'
        receipt = read_json(folder/filename)[0] if filename in members else None
        if receipt is not None and (receipt['request'] != request
                or not utc(reg['received_after']) <= utc(receipt['received_at']) <= now_utc()):
            raise ValueError('receipt request/time differs')
        if status['status'] == 'observed':
            if receipt is None:
                raise ValueError('observed request missing receipt')
            rows = probe.rows_for(request, receipt['data'])
            if status.get('rows') != len(rows):
                raise ValueError('returned row count changed')
            for day, values in rows.items():
                key = (request['code'].split('.')[0], day)
                evidence[key].append({'provider': request['provider'], 'date': day, 'values': values,
                    'request_code': request['code'], 'receipt_file': filename, 'receipt_sha256': members[filename],
                    'received_at': receipt['received_at'],
                    'identity_binding': 'request_only' if request['provider'] == 'hithink_native' else 'returned_code_echo'})
            returns.append({'index': index, 'code': request['code'], 'dates': sorted(rows)})
        statuses.append(status)
    if members != sealed(folder):
        raise ValueError('receipts changed while replaying')
    return reg, members, evidence, statuses, returns


def inventory(con):
    """Diagnostics partition by recorded receipt date, never certify collector origin."""
    category = """CASE WHEN volume IS NULL OR turnover IS NULL OR low IS NULL OR high IS NULL
        OR NOT isfinite(volume) OR NOT isfinite(turnover) OR NOT isfinite(low) OR NOT isfinite(high)
        OR volume<=0 OR turnover<=0 OR low<=0 OR high<low THEN 'invalid_or_zero'
        WHEN turnover*10/volume BETWEEN low*.98 AND high*1.02 THEN 'declared_compatible'
        WHEN turnover/volume BETWEEN low*.98 AND high*1.02 THEN 'equal_scale_candidate'
        ELSE 'other' END"""
    data = con.execute(f"""WITH x AS (SELECT *,{category} category FROM tushare_daily
        WHERE date BETWEEN '2025-01-01' AND '2025-12-31')
        SELECT category,provider,volume_unit,amount_unit,CAST(fetched_at AS DATE) batch_date,
        count(*) row_count,count(DISTINCT stock_code) stocks,min(date) first_day,max(date) last_day
        FROM x GROUP BY ALL ORDER BY category,batch_date,provider,volume_unit,amount_unit""").fetchall()
    columns = ['category', 'stored_provider', 'stored_volume_unit', 'stored_amount_unit',
        'recorded_batch_date', 'rows', 'stocks', 'first_day', 'last_day']
    batches = [dict(zip(columns, [v if v is None or isinstance(v, (int, float)) else str(v) for v in r])) for r in data]
    duplicates = con.execute("""WITH x AS (SELECT stock_code,date,count(*) n FROM tushare_daily
        WHERE date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY ALL HAVING count(*)>1)
        SELECT count(*),count(DISTINCT stock_code),coalesce(sum(n-1),0),coalesce(max(n),0) FROM x""").fetchone()
    return {'batches': batches, 'duplicate_security_days': duplicates[0], 'duplicate_stocks': duplicates[1],
        'excess_rows': duplicates[2], 'max_multiplicity': duplicates[3], 'origin_authenticated': False}


def payload(folder, db):
    before = source_hashes()
    reg, members, evidence, statuses, returns = replay(folder)
    if reg['origin'] != 'native_and_relay':
        raise ValueError('synthetic package cannot qualify native batch units')
    db_hash = file_hash(db)
    if db_hash != reg['database_sha256']:
        raise ValueError('database differs from pre-request registration')
    rows, groups = [], defaultdict(list)
    with duckdb.connect(str(db), read_only=True) as con:
        con.execute('SET threads=2')
        con.execute("SET memory_limit='512MB'")
        diagnostic = inventory(con)
        total = con.execute('SELECT count(*) FROM tushare_daily').fetchone()[0]
        for code, start, end in SAMPLES:
            ticker, exchange = code.split('.')
            records = con.execute('SELECT '+','.join(units.FIELDS)+' FROM tushare_daily WHERE stock_code=? AND date BETWEEN ? AND ? ORDER BY date,ts_code',
                [ticker, start, end]).fetchall()
            if len(records) >= 128:
                raise ValueError('bounded source window required')
            for values in records:
                original = {k: None if v is None else str(v) for k, v in zip(units.FIELDS, values)}
                key = (ticker, original['date'])
                row = units.qualify(original, evidence.get(key, []), exchange=exchange,
                    amount_tolerance=POLICY['amount_fit_tolerance_cny'])
                row['recorded_batch_date'] = (original['fetched_at'] or '')[:10] or None
                row['canonical_qualified'] = False
                groups[key].append(len(rows))
                rows.append(row)
    duplicate_groups = []
    for key, indices in groups.items():
        if len(indices) <= 1:
            continue
        compared = [rows[i] for i in indices]
        qualified = all(r['status'] == 'observed_row_unit_qualified' for r in compared)
        agreement = qualified and (max(number(r['volume_shares']) for r in compared)-min(number(r['volume_shares']) for r in compared) <= number(units.POLICY['volume_tolerance'])
            and max(number(r['turnover_cny']) for r in compared)-min(number(r['turnover_cny']) for r in compared) <= number(POLICY['amount_fit_tolerance_cny']))
        duplicate_groups.append({'stock_code': key[0], 'date': key[1], 'row_indices': indices,
            'all_row_units_qualified': qualified, 'normalized_values_agree_within_tolerance': agreement,
            'merge_authorized': False})
    if before != source_hashes() or db_hash != file_hash(db) or members != sealed(folder):
        raise ValueError('inputs changed during batch analysis')
    counts = dict(Counter(r['status'] for r in rows))
    qualified = counts.get('observed_row_unit_qualified', 0)
    return {'policy': POLICY, 'unit_policy': units.POLICY, 'source_sha256': before,
        'database_sha256': db_hash, 'receipt_manifest_id': identity(members), 'origin': reg['origin'],
        'source_total_rows': total, 'scoped_source_rows': len(rows), 'scoped_security_days': len(groups),
        'qualified_source_rows': qualified, 'unqualified_or_out_of_scope_rows': total-qualified,
        'request_statuses': statuses, 'returned_date_sets': returns,
        'status_counts': counts, 'inventory_2025': diagnostic, 'rows': rows, 'duplicate_groups': duplicate_groups,
        'source_unchanged': True, 'receipts_unchanged': True, 'canonical_rows_published': 0,
        'batch_units_qualified': False, 'global_units_qualified': False, 'merge_authorized': False,
        'identity_qualified': False, 'point_in_time_qualified': False, 'research_ready': False,
        'execution_ready': False, 'production_cutover': False}


def analyze(folder, db, output):
    output = Path(output)
    if output.exists():
        raise ValueError('new output directory required')
    result = payload(folder, db)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'result.json', result)
    seal(output)
    return {k: result[k] for k in ('scoped_source_rows', 'scoped_security_days', 'qualified_source_rows', 'canonical_rows_published', 'status_counts')}


def verify(folder, db, output):
    output = Path(output)
    members = sealed(output)
    if set(members) != {'result.json'} or read_json(output/'result.json')[0] != payload(folder, db) or members != sealed(output):
        raise ValueError('batch report differs from raw input replay')
    return {'verified': True, 'research_ready': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['capture', 'analyze', 'verify'])
    p.add_argument('--db', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--receipts')
    a = p.parse_args()
    if a.action == 'capture':
        capture(a.db, a.output)
    else:
        print(canonical((analyze if a.action == 'analyze' else verify)(a.receipts, a.db, a.output)))
