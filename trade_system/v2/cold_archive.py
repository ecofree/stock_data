"""Bounded incremental price-product archives for isolated rehearsal only.

Copy first. Keep every source row. Overlapping identical rows are read once;
conflicting values under the complete product key block the joint view.
"""
from datetime import date
import json
import os
import re

import duckdb

from trade_system.file_lock import FileLock
from .domain import file_hash, identity
from .isolated_workspace import child, workspace, read_json, write_json, seal, checked
from .recovery_bundle import verified_restore

TABLE = 'multi_source_kline'
KEY = ('source_date', 'asset_type', 'asset_code', 'provider', 'adjustment')
MAX_ROWS = 100000


def _rows(con, query, params=()):
    rows = con.execute(query + ' LIMIT ' + str(MAX_ROWS + 1), params).fetchall()
    if len(rows) > MAX_ROWS:
        raise ValueError('partition/joint-view row budget exceeded; narrow the explicit range')
    columns = [d[0] for d in con.description]
    if not set(KEY).issubset(columns):
        raise ValueError('complete price product key required')
    values = [dict(zip(columns, row)) for row in rows]
    seen = set()
    for row in values:
        key = tuple(row[k] for k in KEY)
        if any(v is None or v == '' for v in key) or key in seen:
            raise ValueError('null/duplicate price product key requires explicit review')
        seen.add(key)
    # Typed schema is checked separately. JSON is only a deterministic value digest.
    return values, _digest(values)


def _digest(values):
    return identity(sorted(json.dumps(r, sort_keys=True, default=str, allow_nan=False) for r in values))


def _schema(con, query, params=()):
    return [[r[0], r[1]] for r in con.execute('DESCRIBE ' + query, params).fetchall()]


def _window(start, end):
    if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start >= end:
        raise ValueError('explicit inclusive start/exclusive end date window required')


def archive(root, source, start, end, restore_directory, *, damaged_receipt_sha256=None, repair_reason=None):
    root = workspace(root)
    db = child(root, source)
    _window(start, end)
    if damaged_receipt_sha256 is not None:
        if not re.fullmatch('[a-f0-9]{64}', damaged_receipt_sha256) or not isinstance(repair_reason, str) or not 1 <= len(repair_reason.strip()) <= 500:
            raise ValueError('explicit damaged receipt hash and bounded repair reason required')
    elif repair_reason is not None:
        raise ValueError('repair reason requires a damaged receipt hash')
    with FileLock(root / 'capabilities.guard'):
        proof = verified_restore(root, restore_directory)
        source_hash = file_hash(db)
        if not any(n.startswith('databases/') and r['sha256'] == source_hash for n, r in proof['files'].items()):
            raise ValueError('source database is not covered by this verified recovery point')
        query = 'SELECT * FROM ' + TABLE + ' WHERE source_date >= ? AND source_date < ?'
        with duckdb.connect(str(db), read_only=True) as con:
            con.execute('BEGIN TRANSACTION')
            values, digest = _rows(con, query, [start, end])
            schema = _schema(con, query, [start, end])
            contract = {'schema': 'isolated_price_archive_v1', 'table': TABLE, 'product_key': list(KEY),
                        'start': start, 'end_exclusive': end, 'columns': schema,
                        'rows': len(values), 'value_sha256': digest}
            partition = identity(contract)
            output = child(root, 'archives/' + partition)
            completion = output / 'completed.json'
            if completion.exists():
                if damaged_receipt_sha256 is None:
                    return verify(root, partition)
                if file_hash(completion) != damaged_receipt_sha256:
                    raise ValueError('damaged receipt changed; explicit review required')
                try:
                    checked(read_json(completion))
                except (ValueError, UnicodeError):
                    pass
                else:
                    raise ValueError('sealed receipt cannot be repaired or replaced')
            elif damaged_receipt_sha256 is not None:
                raise ValueError('no damaged final receipt to repair')
            output.mkdir(parents=True, exist_ok=True)
            # A crash after copying has a deterministic destination. Validate and
            # resume that exact file; never erase or replace partial/bad content.
            parquet = output / 'rows.parquet'
            if not parquet.exists():
                # COPY binds its destination before nested SELECT positional
                # parameters. Validated ISO dates are safe fixed SQL literals.
                export_query = "SELECT * FROM " + TABLE + " WHERE source_date >= DATE '" + start + "' AND source_date < DATE '" + end + "'"
                con.execute('COPY (' + export_query + ') TO ? (FORMAT PARQUET)', [str(parquet)])
            actual, actual_digest = _rows(con, 'SELECT * FROM read_parquet(?)', [str(parquet)])
            if actual_digest != digest or _schema(con, 'SELECT * FROM read_parquet(?)', [str(parquet)]) != schema or len(actual) != len(values):
                raise ValueError('incomplete archive differs; retained for inspection, no overwrite')
            difference = con.execute('''SELECT count(*) FROM (
                (''' + query + ''' EXCEPT ALL SELECT * FROM read_parquet(?)) UNION ALL
                (SELECT * FROM read_parquet(?) EXCEPT ALL ''' + query + '))',
                [start, end, str(parquet), str(parquet), start, end]).fetchone()[0]
            if difference or source_hash != file_hash(db):
                raise ValueError('archive multiset differs or source changed')
        result = seal({**contract, 'partition_id': partition, 'source_sha256': source_hash,
                       'parquet_sha256': file_hash(parquet), 'restore_receipt_id': proof['receipt_id'],
                       'source_deleted': False, 'production_cutover': False, 'execution_ready': False})
        if damaged_receipt_sha256 is not None:
            # Only after source, recovery point, and parquet multiset agree.
            # Preserve the exact bad bytes, and never overwrite a prior record.
            quarantine = child(output, 'failed-receipts/' + damaged_receipt_sha256 + '.json')
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            audit = {'damaged_receipt_sha256': damaged_receipt_sha256, 'reason': repair_reason,
                     'source_sha256': source_hash, 'partition_id': partition,
                     'restore_receipt_id': proof['receipt_id']}
            audit_path = quarantine.with_suffix('.repair.json')
            if audit_path.exists():
                if read_json(audit_path) != audit:
                    raise ValueError('existing receipt repair intent differs')
            else:
                write_json(audit_path, audit)
            if quarantine.exists():
                if file_hash(quarantine) != damaged_receipt_sha256:
                    raise ValueError('quarantined receipt corrupted')
            else:
                os.link(completion, quarantine)
            if file_hash(completion) != damaged_receipt_sha256:
                raise ValueError('damaged receipt changed before quarantine')
            completion.unlink()
        write_json(completion, result)
        return result


def verify(root, partition):
    root = workspace(root)
    if not isinstance(partition, str) or len(partition) != 64 or any(c not in '0123456789abcdef' for c in partition):
        raise ValueError('archive identity required')
    folder = child(root, 'archives/' + partition)
    receipt = checked(read_json(folder / 'completed.json'))
    contract = {k: receipt[k] for k in ('schema', 'table', 'product_key', 'start', 'end_exclusive', 'columns', 'rows', 'value_sha256')}
    if (receipt['schema'] != 'isolated_price_archive_v1' or receipt['table'] != TABLE or
            receipt['product_key'] != list(KEY) or identity(contract) != partition or receipt['partition_id'] != partition):
        raise ValueError('archive contract mismatch')
    parquet = child(folder, 'rows.parquet')
    if file_hash(parquet) != receipt['parquet_sha256']:
        raise ValueError('archive checksum mismatch')
    with duckdb.connect(':memory:') as con:
        values, digest = _rows(con, 'SELECT * FROM read_parquet(?)', [str(parquet)])
        schema = _schema(con, 'SELECT * FROM read_parquet(?)', [str(parquet)])
    if len(values) != receipt['rows'] or digest != receipt['value_sha256'] or identity(schema) != identity(receipt['columns']):
        raise ValueError('archive data controls mismatch')
    if any(not receipt['start'] <= str(v['source_date']) < receipt['end_exclusive'] for v in values):
        raise ValueError('archive row outside its date window')
    return receipt


def _partition_list(partitions):
    if not isinstance(partitions, list) or len(partitions) != len(set(partitions)) or len(partitions) > 100:
        raise ValueError('bounded unique archive list required')


def _baseline(root, restore_directory, database, start, end):
    proof = verified_restore(root, restore_directory)
    if database not in proof['files'] or not database.startswith('databases/'):
        raise ValueError('coverage baseline must be a restored database')
    path = child(root, restore_directory + '/' + database)
    query = 'SELECT * FROM ' + TABLE + ' WHERE source_date >= ? AND source_date < ?'
    with duckdb.connect(str(path), read_only=True) as con:
        con.execute('BEGIN')
        rows, digest = _rows(con, query, [start, end])
        columns = _schema(con, query, [start, end])
    return proof, {'columns': columns, 'rows': len(rows), 'value_sha256': digest}


def plan_coverage(root, source, partitions, start, end, restore_directory, destination):
    """Seal a bounded query baseline and explicit partition catalog, not a guess
    from whatever files happen to survive in the archive directory.
    """
    root = workspace(root)
    _window(start, end)
    _partition_list(partitions)
    with FileLock(root / 'capabilities.guard'):
        proof = verified_restore(root, restore_directory)
        source_hash = file_hash(child(root, source))
        matches = sorted(n for n, r in proof['files'].items() if n.startswith('databases/') and r['sha256'] == source_hash)
        if not matches:
            raise ValueError('coverage source is not covered by this verified recovery point')
        database = matches[0]
        proof, baseline = _baseline(root, restore_directory, database, start, end)
        catalog = []
        for partition in sorted(partitions):
            receipt = verify(root, partition)
            if receipt['columns'] != baseline['columns']:
                raise ValueError('coverage archive schema differs')
            catalog.append({'partition_id': partition, 'receipt_id': receipt['receipt_id']})
        result = seal({'schema': 'isolated_price_coverage_v1', 'table': TABLE, 'product_key': list(KEY),
                       'start': start, 'end_exclusive': end, **baseline,
                       'baseline_database': database, 'source_sha256': source_hash,
                       'restore_directory': restore_directory, 'restore_receipt_id': proof['receipt_id'],
                       'partitions': catalog, 'scope': 'verified_snapshot_only', 'execution_ready': False})
        write_json(child(root, destination), result)
    return result


def _coverage(root, name, partitions, start, end):
    if not name:
        raise ValueError('verified coverage plan required; an explicit partition list alone cannot prove completeness')
    value = checked(read_json(child(root, name)))
    if (value['schema'] != 'isolated_price_coverage_v1' or value['table'] != TABLE or value['product_key'] != list(KEY)
            or value['start'] != start or value['end_exclusive'] != end
            or value['scope'] != 'verified_snapshot_only' or value['execution_ready'] is not False):
        raise ValueError('coverage contract or query window differs')
    catalog = value['partitions']
    if not isinstance(catalog, list) or len(catalog) > 100:
        raise ValueError('bounded coverage catalog required')
    ids = [r['partition_id'] for r in catalog]
    _partition_list(ids)
    if set(ids) != set(partitions):
        raise ValueError('coverage partition catalog mismatch: missing or unexpected partition')
    proof, baseline = _baseline(root, value['restore_directory'], value['baseline_database'], start, end)
    if (proof['receipt_id'] != value['restore_receipt_id']
            or proof['files'][value['baseline_database']]['sha256'] != value['source_sha256']
            or any(value[k] != v for k, v in baseline.items())):
        raise ValueError('coverage baseline differs from the actual recovery point')
    for item in catalog:
        if verify(root, item['partition_id'])['receipt_id'] != item['receipt_id']:
            raise ValueError('coverage partition receipt changed')
    return value


def joint_read(root, source, partitions, start, end, *, coverage=None):
    root = workspace(root)
    _window(start, end)
    _partition_list(partitions)
    with FileLock(root / 'capabilities.guard'):
        expected = _coverage(root, coverage, partitions, start, end)
        receipts = [verify(root, p) for p in partitions]
        with duckdb.connect(str(child(root, source)), read_only=True) as con:
            con.execute('BEGIN TRANSACTION')
            query = 'SELECT * FROM ' + TABLE + ' WHERE source_date >= ? AND source_date < ?'
            values, _ = _rows(con, query, [start, end])
            schema = _schema(con, query, [start, end])
            for receipt in receipts:
                if identity(receipt['columns']) != identity(schema):
                    raise ValueError('hot/cold schemas differ')
                rows, _ = _rows(con, 'SELECT * FROM read_parquet(?) WHERE source_date >= ? AND source_date < ?',
                                [str(child(root, 'archives/' + receipt['partition_id'] + '/rows.parquet')), start, end])
                values.extend(rows)
                if len(values) > MAX_ROWS:
                    raise ValueError('joint-view row budget exceeded')
        result = {}
        overlaps = 0
        for row in values:
            key = tuple(row[k] for k in KEY)
            if key in result:
                if result[key] != row:
                    raise ValueError('hot/cold product conflict; explicit revision decision required')
                overlaps += 1
            result[key] = row
        rows = [result[k] for k in sorted(result)]
        if schema != expected['columns'] or len(rows) != expected['rows'] or _digest(rows) != expected['value_sha256']:
            raise ValueError('joint-view coverage differs from verified baseline; missing, extra or revised data')
        return {'scope': 'isolated_hot_cold_joint_read', 'rows': rows, 'identical_overlap_rows': overlaps,
                'coverage_verified': True, 'coverage_receipt_id': expected['receipt_id'],
                'coverage_scope': 'verified_snapshot_only', 'value_sha256': expected['value_sha256'],
                'source_deleted': False, 'execution_ready': False}


def retention_plan(root, partitions):
    root = workspace(root)
    receipts = [verify(root, p) for p in partitions]
    return {'scope': 'isolated_retention_preparation', 'partitions': [
        {'partition_id': r['partition_id'], 'rows': r['rows'], 'action': 'retain_source_and_archive',
         'reason': 'offsite_restore_retention_period_and_source_retirement_not_approved'} for r in receipts],
        'deletions': 0, 'production_cutover': False}
