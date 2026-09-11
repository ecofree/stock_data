"""Native-authority canonical price slice and v7 diagnostic export; no training approval."""
import argparse
from collections import defaultdict
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import uuid

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2 import probe_price_unit_batches as batch
from trade_system.file_lock import FileLock
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import canonical, file_hash, identity, number
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_receipts import sealed

POLICY = {'version': 'native_reconciled_price_slice_v1',
    'authority': 'hithink_native_same_request_code_and_date',
    'legacy_selection': 'none_all_variants_retained_and_reconciled',
    'cross_variant_volume_tolerance': '0.000001', 'cross_variant_amount_tolerance_cny': '0.50',
    'conflict': 'quarantine_entire_security_day', 'scope': 'registered_observations_only_not_identity_merge',
    'price_adjustment': 'none', 'volume_unit': 'shares', 'amount_unit': 'CNY'}
FEATURES = ['open', 'high', 'low', 'close', 'volume', 'turnover']


def resolve(rows):
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
                for field, tolerance in [('volume_shares', POLICY['cross_variant_volume_tolerance']),
                                         ('turnover_cny', POLICY['cross_variant_amount_tolerance_cny'])])
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


def payload(receipts, db):
    source_hash = file_hash(Path(__file__))
    report = batch.payload(receipts, db)
    records = resolve(report['rows'])
    if len(records) != report['scoped_security_days']:
        raise ValueError('canonical cohort cardinality differs')
    if source_hash != file_hash(Path(__file__)):
        raise ValueError('canonical source changed')
    return {'policy': POLICY, 'source_sha256': {**report['source_sha256'], Path(__file__).name: source_hash},
        'database_sha256': report['database_sha256'], 'receipt_manifest_id': report['receipt_manifest_id'],
        'origin': report['origin'], 'source_total_rows': report['source_total_rows'],
        'scoped_source_rows': report['scoped_source_rows'], 'scoped_security_days': len(records),
        'canonical_rows': sum(r['status']=='canonical_price_observation' for r in records),
        'quarantined_rows': sum(r['status']=='quarantined' for r in records),
        'reconciled_duplicate_groups': sum(r['status']=='canonical_price_observation' and r['source_variant_count']>1 for r in records),
        'uncovered_source_rows': report['source_total_rows']-report['scoped_source_rows'],
        'records': records, 'raw_records_deleted': 0, 'database_modified': False,
        'global_units_qualified': False, 'identity_merge_authorized': False,
        'point_in_time_qualified': False, 'research_ready': False, 'execution_ready': False, 'production_cutover': False}


def build(receipts, db, output):
    output = Path(output)
    if output.exists():
        raise ValueError('new canonical output directory required')
    result = payload(receipts, db)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'canonical-prices.json', result)
    seal(output)
    return {k: result[k] for k in ('canonical_rows', 'quarantined_rows', 'reconciled_duplicate_groups', 'research_ready')}


def verify(folder, receipts, db):
    folder = Path(folder)
    members = sealed(folder)
    if set(members) != {'canonical-prices.json'}:
        raise ValueError('exact canonical layer members required')
    result = read_json(folder/'canonical-prices.json')[0]
    if result != payload(receipts, db) or members != sealed(folder):
        raise ValueError('canonical layer differs from raw source replay')
    return result


def diagnostic_rows(con, report):
    """Retain every scoped key. Price paths are diagnostic only, never default labels."""
    records = report['records']
    if not records:
        raise ValueError('empty canonical cohort')
    start, end = min(r['date'] for r in records), max(r['date'] for r in records)
    calendar = con.execute("SELECT CAST(cal_date AS VARCHAR),is_open FROM tushare_trade_cal WHERE exchange='SSE' AND cal_date BETWEEN ? AND ? ORDER BY cal_date", [start, end]).fetchall()
    expected = [(date.fromisoformat(start)+timedelta(days=i)).isoformat() for i in range((date.fromisoformat(end)-date.fromisoformat(start)).days+1)]
    if [r[0] for r in calendar] != expected or any(type(r[1]) is not bool for r in calendar):
        raise ValueError('complete unique stored daily calendar required for diagnostic paths')
    opened = [d for d, is_open in calendar if is_open]
    seq = {d: i for i, d in enumerate(opened)}
    lookup = {(r['stock_code'], r['date']): r for r in records}
    if len(lookup) != len(records):
        raise ValueError('duplicate canonical security-day')
    result = []
    for record in records:
        code, day = record['stock_code'], record['date']
        values = record['values']
        row = {'datetime': day, 'instrument': code,
            **{k: float(number(values[k])) if values else None for k in ('open','high','low','close')},
            'volume': float(number(values['volume_shares'])) if values else None,
            'turnover': float(number(values['turnover_cny'])) if values else None,
            'canonical_record_id': record['record_id'], 'price_status': record['status'],
            'source_variant_count': record['source_variant_count'],
            'raw_price_target_ret': None, 'raw_price_target_date': None,
            'label_next_ret': None, 'label_date': None,
            'label_status': 'blocked_unqualified_adjustment_identity_and_calendar',
            'volume_z20': None, 'warmup_price_observations_20d': 0}
        i = seq.get(day)
        if values is not None and i is not None:
            if i+2 < len(opened):
                n, n2 = [lookup.get((code, opened[i+j])) for j in (1,2)]
                if n and n2 and n['values'] and n2['values']:
                    row['raw_price_target_ret'] = float((number(n2['values']['close'])/number(n['values']['open'])-1)*100)
                    row['raw_price_target_date'] = opened[i+2]
            # Number of exact preceding calendar sessions covered, not a partial-window feature.
            row['warmup_price_observations_20d'] = sum(bool(lookup.get((code,d), {}).get('values')) for d in opened[max(0,i-19):i+1])
        if values is None:
            row['label_status'] = 'blocked_quarantined_price'
        result.append(row)
    return result


def export_features(db, output, *, layer, receipts, start_date=None, end_date=None, output_format='csv'):
    """Explicit six-field v7 price input, not silent fallback to legacy 21-field training data."""
    if output_format not in ('csv', 'parquet', 'both'):
        raise ValueError('unsupported export format')
    out = Path(output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(out.with_suffix('.export.guard')):
        report = verify(layer, receipts, db)
        layer_members, receipt_members = sealed(layer), sealed(receipts)
        exporter_hash = file_hash(Path(__file__).parents[2]/'scripts/export_qlib_features.py')
        source_hash = file_hash(Path(__file__))
        with duckdb.connect(str(db), read_only=True) as con:
            con.execute('SET threads=2')
            con.execute("SET memory_limit='512MB'")
            rows = diagnostic_rows(con, report)
            start = start_date or min(r['datetime'] for r in rows)
            end = end_date or max(r['datetime'] for r in rows)
            date.fromisoformat(start); date.fromisoformat(end)
            if not min(r['datetime'] for r in rows) <= start <= end <= max(r['datetime'] for r in rows):
                raise ValueError('export outside registered canonical price scope')
            selected = [r for r in rows if start <= r['datetime'] <= end]
            if not selected:
                raise ValueError('no registered security-days in export interval')
            fields = list(selected[0])
            text_fields = {'datetime','instrument','canonical_record_id','price_status','raw_price_target_date','label_date','label_status'}
            schema = ','.join(f'{k} '+('VARCHAR' if k in text_fields else 'DOUBLE') for k in fields)
            con.execute('CREATE TEMP TABLE canonical_export('+schema+')')
            con.executemany('INSERT INTO canonical_export VALUES ('+','.join('?' for _ in fields)+')', [tuple(row[k] for k in fields) for row in selected])
            count, unique, labels = con.execute('SELECT count(*),count(DISTINCT (datetime,instrument)),count(label_next_ret) FROM canonical_export').fetchone()
            if count != unique or count != len(selected) or labels:
                raise ValueError('canonical export row/label invariant failed')
            run = out.parent/(out.stem+'.versions')/uuid.uuid4().hex
            run.mkdir(parents=True, exist_ok=False)
            outputs = {}
            for fmt in ('csv','parquet') if output_format=='both' else (output_format,):
                target = run/out.with_suffix('.'+fmt).name
                settings = 'FORMAT CSV, HEADER TRUE' if fmt=='csv' else 'FORMAT PARQUET'
                con.execute("COPY (SELECT * FROM canonical_export ORDER BY datetime,instrument) TO '"+str(target).replace("'","''")+"' ("+settings+")")
                outputs[fmt] = str(target)
        if (layer_members != sealed(layer) or receipt_members != sealed(receipts)
                or file_hash(db) != report['database_sha256'] or source_hash != file_hash(Path(__file__))
                or any(report['source_sha256'][k] != v for k,v in batch.source_hashes().items())
                or exporter_hash != file_hash(Path(__file__).parents[2]/'scripts/export_qlib_features.py')):
            raise ValueError('canonical inputs changed before publication')
        metadata = {'label_version': 'native_canonical_v7_price_diagnostic_no_training_labels',
            'scope': 'bounded_retrospective_price_input_not_qlib_training_acceptance',
            'format': output_format, 'outputs': outputs, 'rows': len(selected),
            'instruments': len({r['instrument'] for r in selected}), 'labeled_rows': 0,
            'raw_price_proxy_rows': sum(r['raw_price_target_ret'] is not None for r in selected),
            'start_date': start, 'end_date': end, 'feature_columns': FEATURES,
            'label_column': 'label_next_ret', 'label_policy': 'all_null_until_separate_adjustment_identity_calendar_protocol',
            'volume_unit': 'shares', 'turnover_unit': 'CNY', 'price_adjustment': 'none',
            'moneyflow_consumed': False, 'daily_basic_consumed': False,
            'legacy_source_fallback': False, 'calendar_evidence': 'stored_SSE_only_not_native_or_SZSE_qualification',
            'leakage_guard': 'raw_price_target_ret/raw_price_target_date and provenance/status columns excluded from feature_columns',
            'warmup_policy': 'sparse_registered_samples_not_full_20_session_window; volume_z20_disabled',
            'cohort_policy': 'registered_slice_only; quarantined_keys_retained_with_null_prices; not_full_market',
            'canonical_layer_manifest_id': identity(layer_members), 'receipt_manifest_id': identity(receipt_members),
            'database_sha256': report['database_sha256'], 'source_sha256': {**report['source_sha256'], 'export_qlib_features.py': exporter_hash},
            'scoped_source_rows': report['scoped_source_rows'], 'full_scoped_security_days': report['scoped_security_days'],
            'uncovered_source_rows': report['uncovered_source_rows'],
            'artifact_hashes': {Path(p).name: file_hash(p) for p in outputs.values()},
            'research_ready': False, 'execution_ready': False, 'production_cutover': False}
        meta_path = run/out.with_suffix('.metadata.json').name
        write_json(meta_path, metadata)
        pointer = {'outputs': {fmt: Path(p).relative_to(out.parent).as_posix() for fmt,p in outputs.items()},
            'metadata_sha256': file_hash(meta_path)}
        temp = out.with_suffix('.current.'+uuid.uuid4().hex+'.tmp')
        with temp.open('x', encoding='utf-8') as handle:
            json.dump(pointer, handle); handle.flush(); os.fsync(handle.fileno())
        temp.replace(out.with_suffix('.current.json'))
        return metadata


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['build','verify'])
    p.add_argument('--receipts', required=True); p.add_argument('--db', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    result = build(a.receipts,a.db,a.output) if a.action=='build' else {'verified': bool(verify(a.output,a.receipts,a.db))}
    print(canonical(result))
