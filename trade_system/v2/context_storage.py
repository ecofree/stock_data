"""Versioned context migration and immutable input/output binding."""
import hashlib
import json

from .domain import canonical, identity, utc
from .market_context import ContextPolicy, build_context


MIGRATION_VERSION = 2
MIGRATION_SQL = '''CREATE TABLE market_context_snapshot(
 context_id VARCHAR PRIMARY KEY, trade_date DATE NOT NULL, asof_time TIMESTAMPTZ NOT NULL,
 mode VARCHAR NOT NULL, raw_hash VARCHAR NOT NULL, input_hash VARCHAR NOT NULL,
 output_hash VARCHAR NOT NULL, policy_version VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL,
 payload JSON NOT NULL)'''


def _schema_present(store):
    store.check_owner()
    row = store.con.execute('SELECT sha256 FROM v2_schema WHERE version=?', [MIGRATION_VERSION]).fetchone()
    if row and row[0] != hashlib.sha256(MIGRATION_SQL.encode()).hexdigest():
        raise ValueError('context migration checksum mismatch')
    return row is not None


def import_context(store, bundle, policy):
    store.check_owner()
    if utc(bundle['asof']) > utc(store.clock()):
        raise ValueError('future context cannot be committed')
    policy = ContextPolicy(**policy)
    result = build_context(bundle, policy)
    key = identity(result)
    raw_hash = store.archive(canonical(bundle).encode())
    with store.transaction():
        if not _schema_present(store):
            store.con.execute(MIGRATION_SQL)
            store.con.execute('INSERT INTO v2_schema VALUES (?,?)',
                              [MIGRATION_VERSION,hashlib.sha256(MIGRATION_SQL.encode()).hexdigest()])
        store.con.execute('''INSERT INTO market_context_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT DO NOTHING''',
                          [key,bundle['trade_date'],utc(bundle['asof']),bundle['mode'],raw_hash,
                           result['input_hash'],key,policy.version,utc(store.clock()),canonical(result)])
    return {'context_id':key,'trade_date':bundle['trade_date'],'mode':bundle['mode'],
            'themes':len(result['theme_roles']),'candidates':len(result['conditional_candidates']),
            'execution_ready':False}


def get_context(store, context_id):
    if not _schema_present(store):
        raise ValueError('context not imported')
    row = store.con.execute('SELECT payload,output_hash,raw_hash FROM market_context_snapshot WHERE context_id=?', [context_id]).fetchone()
    if row is None:
        raise ValueError('unknown context')
    data = json.loads(row[0])
    if identity(data) != row[1] or row[1] != context_id:
        raise ValueError('context output checksum mismatch')
    raw_path = store.path.parent / (store.path.name+'.raw') / row[2]
    raw = raw_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row[2] or identity(json.loads(raw)) != data['input_hash']:
        raise ValueError('context raw input checksum mismatch')
    return data
