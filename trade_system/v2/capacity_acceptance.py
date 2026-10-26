"""Bounded raw historical-table preservation, without data qualification or deletion.

Uses snapshot-local row ranges only to partition an immutable restored database.
Duplicate/unknown business keys remain byte/value evidence, never cleaned facts.
"""
import math
import time
import duckdb

from trade_system.file_lock import FileLock
from trade_system.business_contract import PRODUCTS
from .domain import file_hash
from .isolated_workspace import workspace, child, read_json, write_json, seal, checked
from .recovery_bundle import verified_restore

MAX_BATCH_ROWS = 100000
MAX_BATCHES = 200


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def connect(path, root):
    return duckdb.connect(str(path), read_only=True, config={
        'threads': '1', 'memory_limit': '512MB',
        'temp_directory': str(child(root, 'capacity-temp')), 'max_temp_directory_size': '1GB'})


def inventory(root, source):
    root = workspace(root)
    with connect(child(root, source), root) as con:
        con.execute('BEGIN')
        tables = con.execute("SELECT table_name FROM duckdb_tables() WHERE NOT internal AND schema_name='main' ORDER BY table_name").fetchall()
        rows = []
        for (name,) in tables:
            products = sorted(k for k, v in PRODUCTS.items() if name in v.raw)
            rows.append({'table': name, 'rows': con.execute('SELECT count(*) FROM ' + quote(name)).fetchone()[0],
                         'products': products, 'decision': 'preserve_source',
                         'reason': 'no_source_retirement_or_reconstruction_evidence'})
    return {'schema': 'historical_capacity_inventory_v1', 'tables': rows,
            'source_deleted': False, 'execution_ready': False}


def baseline(root, restored, source_hash):
    proof = verified_restore(root, restored)
    matches = sorted(k for k, v in proof['files'].items() if k.startswith('databases/') and v['sha256'] == source_hash)
    if not matches:
        raise ValueError('capacity source must match an actually verified restored database')
    return child(root, restored + '/' + matches[0]), proof


def specification(con, table, batch_rows):
    if type(batch_rows) is not int or not 1 <= batch_rows <= MAX_BATCH_ROWS:
        raise ValueError('bounded batch rows required')
    if (table,) not in con.execute("SELECT table_name FROM duckdb_tables() WHERE NOT internal AND schema_name='main'").fetchall():
        raise ValueError('explicit physical main table required')
    schema = [list(r) for r in con.execute('DESCRIBE SELECT * FROM ' + quote(table)).fetchall()]
    if any(r[0].lower() == 'rowid' for r in schema):
        raise ValueError('shadowed snapshot row identity is unsupported')
    count, first, last = con.execute('SELECT count(*),min(rowid),max(rowid) FROM ' + quote(table)).fetchone()
    if count and math.ceil((last - first + 1) / batch_rows) > MAX_BATCHES:
        raise ValueError('snapshot exceeds bounded batch count')
    ranges = [[a, min(a + batch_rows, last + 1)] for a in range(first, last + 1, batch_rows)] if count else []
    return {'table': table, 'columns': schema, 'rows': count, 'ranges': ranges, 'batch_rows': batch_rows}


def equal_batch(con, query, parquet):
    expected = con.execute('SELECT count(*) FROM (' + query + ')').fetchone()[0]
    actual = con.execute('SELECT count(*) FROM read_parquet(?)', [str(parquet)]).fetchone()[0]
    schema = con.execute('DESCRIBE ' + query).fetchall()
    other = con.execute('DESCRIBE SELECT * FROM read_parquet(?)', [str(parquet)]).fetchall()
    # Nullability/key metadata is not preserved by parquet; column names/types are.
    if [(r[0], r[1]) for r in schema] != [(r[0], r[1]) for r in other] or expected != actual:
        raise ValueError('batch rows or types differ')
    difference = con.execute('SELECT count(*) FROM ((' + query + ' EXCEPT ALL SELECT * FROM read_parquet(?)) UNION ALL (SELECT * FROM read_parquet(?) EXCEPT ALL ' + query + '))', [str(parquet), str(parquet)]).fetchone()[0]
    if difference:
        raise ValueError('batch business values or multiplicity differ')
    return actual


def archive_table(root, source, table, restored, destination, *, batch_rows=MAX_BATCH_ROWS):
    root = workspace(root)
    folder = child(root, destination)
    started = time.monotonic()
    with FileLock(root / 'capabilities.guard'):
        source_hash = file_hash(child(root, source))
        restored_db, proof = baseline(root, restored, source_hash)
        with connect(restored_db, root) as con:
            con.execute('BEGIN')
            spec = specification(con, table, batch_rows)
            contract = {'schema': 'raw_historical_table_archive_v1', 'source_sha256': source_hash, **spec,
                        'scope': 'raw_snapshot_preservation_not_qualified_market_facts',
                        'source_deleted': False, 'execution_ready': False}
            folder.mkdir(parents=True, exist_ok=True)
            intent = folder / 'intent.json'
            if intent.exists():
                if checked(read_json(intent)) != seal(contract):
                    raise ValueError('existing archive intent differs')
            else:
                if list(folder.iterdir()):
                    raise ValueError('unowned nonempty capacity destination')
                write_json(intent, seal(contract))
            parts = []
            for index, (start, end) in enumerate(spec['ranges']):
                name = f'{index:06d}.parquet'
                path = child(folder, name)
                query = 'SELECT * FROM ' + quote(table) + f' WHERE rowid >= {start} AND rowid < {end}'
                if not path.exists():
                    con.execute('COPY (' + query + ') TO ? (FORMAT PARQUET, COMPRESSION ZSTD)', [str(path)])
                rows = equal_batch(con, query, path)
                part = {'file': name, 'start': start, 'end': end, 'rows': rows,
                        'bytes': path.stat().st_size, 'sha256': file_hash(path)}
                receipt = path.with_suffix('.json')
                if receipt.exists():
                    if checked(read_json(receipt)) != seal(part):
                        raise ValueError('batch receipt changed')
                else:
                    write_json(receipt, seal(part))
                parts.append(part)
        if file_hash(child(root, source)) != source_hash:
            raise ValueError('source changed during capacity acceptance')
        result = seal({**contract, 'parts': parts, 'restore_receipt_id': proof['receipt_id']})
        final = folder / 'completed.json'
        if final.exists():
            if checked(read_json(final)) != result:
                raise ValueError('completed archive differs')
        else:
            write_json(final, result)
    return {'catalog': destination, 'receipt_id': result['receipt_id'], 'rows': spec['rows'],
            'batches': len(parts), 'archive_bytes': sum(p['bytes'] for p in parts),
            'seconds': time.monotonic() - started, 'source_deleted': False}


def verify_table(root, catalog, restored):
    root = workspace(root)
    folder = child(root, catalog)
    started = time.monotonic()
    with FileLock(root / 'capabilities.guard'):
        value = checked(read_json(folder / 'completed.json'))
        if (value['schema'] != 'raw_historical_table_archive_v1'
                or value['scope'] != 'raw_snapshot_preservation_not_qualified_market_facts'
                or value['source_deleted'] is not False or value['execution_ready'] is not False):
            raise ValueError('wrong historical capacity contract')
        db, _ = baseline(root, restored, value['source_sha256'])
        with connect(db, root) as con:
            con.execute('BEGIN')
            spec = specification(con, value['table'], value['batch_rows'])
            if any(value[k] != v for k, v in spec.items()):
                raise ValueError('catalog differs from independent restored baseline')
            parts = value['parts']
            if len(parts) != len(spec['ranges']) or {p.name for p in folder.glob('*.parquet')} != {p['file'] for p in parts}:
                raise ValueError('missing or extra archive partition')
            for index, ((start, end), part) in enumerate(zip(spec['ranges'], parts)):
                if part['file'] != f'{index:06d}.parquet' or (part['start'], part['end']) != (start, end):
                    raise ValueError('partition coverage is missing, repeated or reordered')
                path = child(folder, part['file'])
                if file_hash(path) != part['sha256'] or path.stat().st_size != part['bytes']:
                    raise ValueError('archive bytes changed')
                if checked(read_json(path.with_suffix('.json'))) != seal(part):
                    raise ValueError('batch receipt differs')
                query = 'SELECT * FROM ' + quote(value['table']) + f' WHERE rowid >= {start} AND rowid < {end}'
                if equal_batch(con, query, path) != part['rows']:
                    raise ValueError('batch count differs')
        if sum(p['rows'] for p in parts) != value['rows']:
            raise ValueError('total coverage differs')
    return {'schema': 'raw_historical_capacity_verification_v1', 'catalog_id': value['receipt_id'],
            'rows': value['rows'], 'batches': len(parts), 'archive_bytes': sum(p['bytes'] for p in parts),
            'seconds': time.monotonic() - started, 'complete_multiset_verified': True,
            'qualified_business_keys_certified': False, 'source_deleted': False, 'execution_ready': False}
