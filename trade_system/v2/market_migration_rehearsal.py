"""Explicit historical copies only: migrate, preserve facts, report qualification.

This is an offline rehearsal, never a production activation or repair entry.
An unsuccessful candidate remains in its fresh evidence directory with a failed
receipt. Original rows, unknown identities and legacy checksum uncertainty stay.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

import duckdb

from trade_system.market_contract import market_daily_status_sql
from trade_system.migrations import (
    apply_pending_in_transaction, applied_versions, discover_migrations,
)
from trade_system.normalize import build_normalized_views_on_connection
from trade_system.v2.market_workspace import price_observation_reason_sql

APPROVED_VERSIONS = (14, 15, 16, 17)
_BOOKKEEPING = {'schema_migration', 'schema_migration_checksum'}
_ADDITIONS = {
    'qlib_shadow_evaluation': {
        'evaluation_method', 'quantile', 'round_trip_cost_bps', 'updated_at',
        'avg_net_return', 'net_top_bottom_spread', 'top_quantile_net_return', 'bottom_quantile_net_return',
    },
    'tushare_index_daily': {'volume_unit', 'amount_unit', 'adjustment', 'provider'},
    'multi_source_kline': {'adjustment'},
    'daily_summary': {'count_provenance_json'},
}
_NEW_TABLES = _BOOKKEEPING | {'qlib_shadow_evaluation', 'multi_source_kline'}
_OLD_INDEXES = {'uq_multi_source_kline', 'ix_multi_source_kline'}
_PRODUCT_INDEX = 'uq_multi_source_kline_product'
_INDEX_OWNER = {'schema': 'main', 'table': 'multi_source_kline'}
_LEGACY_KEYS = ['source_date', 'asset_type', 'asset_code', 'provider']
# Pre-0016 schema._ensure_business_indexes used this unique business key,
# falling back to the same four-column non-unique ix_ index on duplicates.
_INDEX_DEFINITIONS = {
    'uq_multi_source_kline': {'is_unique': True, 'expressions': _LEGACY_KEYS},
    'ix_multi_source_kline': {'is_unique': False, 'expressions': _LEGACY_KEYS},
    _PRODUCT_INDEX: {'is_unique': True, 'expressions': [*_LEGACY_KEYS, 'adjustment']},
}


def _sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _q(value):
    return '"' + str(value).replace('"', '""') + '"'


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _index_definition(unique, expressions):
    return {'is_unique': bool(unique),
            'expressions': [part.strip().strip('"') for part in expressions.strip('[]').split(',')]}


def _no_links(path):
    """Inspect lexical ancestors before resolve, including Windows reparses."""
    path = Path(os.path.abspath(path))
    for member in (path, *path.parents):
        try:
            info = member.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('input/output path traverses a link or reparse point')
    return path


def _validate_paths(source, output, expected):
    if not re.fullmatch('[a-fA-F0-9]{64}', expected or ''):
        raise ValueError('externally supplied source SHA-256 is required')
    source, output = _no_links(source), _no_links(output)
    if not source.is_file() or source.suffix.lower() != '.duckdb':
        raise ValueError('source must be an existing historical .duckdb file')
    if output.exists():
        raise FileExistsError('output must be a fresh isolated directory')
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('source and output overlap')
    if not output.parent.is_dir():
        raise ValueError('output parent must already exist')
    # Avoid routing explicit arguments into a live market or source checkout.
    from trade_system.db_utils import primary_checkout
    roots = {Path(__file__).resolve().parents[2], primary_checkout()}
    runtime = os.environ.get('STOCKDATA_RUNTIME_ROOT')
    if runtime:
        roots.add(Path(runtime).resolve())
    for path in (source, output):
        if any(root and (path == root or root in path.parents) for root in roots):
            raise ValueError('rehearsal paths must be outside project/production roots')
        if 'stock_data-runtime' in {part.lower() for part in path.parts}:
            raise ValueError('rehearsal refuses production runtime paths')
    if Path(str(source)+'.wal').exists():
        raise ValueError('source WAL exists; require a quiescent historical original')
    return source, output


@contextmanager
def _source_read_guard(source):
    """On Windows deny writes/deletion while validating and copying original."""
    if os.name != 'nt':
        with source.open('rb'):
            yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(source), 0x80000000, 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), 'historical source cannot be held read-only')
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    try:
        yield
    finally:
        close(handle)


def _snapshot(con):
    database = con.execute('SELECT current_database()').fetchone()[0]
    tables = {}
    for schema, table, sql in con.execute(
            'SELECT schema_name,table_name,sql FROM duckdb_tables() '
            'WHERE database_name=? AND NOT internal ORDER BY schema_name,table_name', [database]).fetchall():
        if schema != 'main':
            raise ValueError('unreviewed non-main historical schema')
        columns = con.execute(f'PRAGMA table_info({_literal(table)})').fetchall()
        tables[table] = {'columns': [list(r) for r in columns], 'sql': sql,
                         'rows': con.execute(f'SELECT count(*) FROM main.{_q(table)}').fetchone()[0]}
    objects = {}
    for kind, function, field in [('indexes', 'duckdb_indexes()', 'index_name'),
                                  ('views', 'duckdb_views()', 'view_name')]:
        internal = ' AND NOT internal' if kind == 'views' else ''
        objects[kind] = dict(con.execute(
            f'SELECT {field},sql FROM {function} WHERE database_name=?{internal}', [database]).fetchall())
    constraints = [list(row) for row in con.execute(
        'SELECT table_name,constraint_type,constraint_text,constraint_column_names '
        'FROM duckdb_constraints() WHERE database_name=? ORDER BY table_name,constraint_index', [database]).fetchall()]
    index_owners = {name: {'schema': schema, 'table': table} for name, schema, table in con.execute(
        'SELECT index_name,schema_name,table_name FROM duckdb_indexes() WHERE database_name=?', [database]).fetchall()}
    index_definitions = {name: _index_definition(unique, expressions) for name, unique, expressions in con.execute(
        'SELECT index_name,is_unique,expressions FROM duckdb_indexes() WHERE database_name=?', [database]).fetchall()}
    return {'tables': tables, 'constraints': constraints, 'index_owners': index_owners,
            'index_definitions': index_definitions, **objects}


def _check_reserved_index_ownership(snapshot):
    for name, owner in snapshot['index_owners'].items():
        if name in _OLD_INDEXES | {_PRODUCT_INDEX} and owner != _INDEX_OWNER:
            raise ValueError('index ownership conflict: reserved migration index belongs to another relation')
        if name in _INDEX_DEFINITIONS and snapshot['index_definitions'][name] != _INDEX_DEFINITIONS[name]:
            raise ValueError('index definition conflict: reserved migration index has an unreviewed definition')


def _allowed_removed_indexes(snapshot):
    return {name for name, owner in snapshot['index_owners'].items()
            if name in _OLD_INDEXES and owner == _INDEX_OWNER
            and snapshot['index_definitions'][name] == _INDEX_DEFINITIONS[name]}


def _duplicate_products(con):
    tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
    if 'multi_source_kline' not in tables:
        return {'groups': 0, 'excess_rows': 0, 'scope': 'table_absent'}
    cols = {r[0] for r in con.execute('DESCRIBE multi_source_kline').fetchall()}
    keys = ('source_date', 'asset_type', 'asset_code', 'provider', 'adjustment')
    expressions = [_q(k) if k in cols else 'NULL' for k in keys]
    groups, excess = con.execute('SELECT count(*),coalesce(sum(n-1),0) FROM ('
        'SELECT count(*) n FROM multi_source_kline GROUP BY '+','.join(expressions)+' HAVING count(*)>1)').fetchone()
    # No raw keys or payloads are emitted: this is a bounded aggregate conflict list.
    samples = con.execute('SELECT '+','.join(expressions)+',count(*) n FROM multi_source_kline GROUP BY '
        + ','.join(expressions)+' HAVING count(*)>1 ORDER BY ALL LIMIT 100').fetchall() if groups else []
    return {'groups': groups, 'excess_rows': excess, 'scope': 'full_product_key_including_null_groups',
            'group_samples': [dict(zip((*keys, 'rows'), row)) for row in samples],
            'sample_limit': 100, 'samples_truncated': groups > 100}


def _preservation(con, before, *, rebuilt_views):
    after = _snapshot(con)
    removable_indexes = _allowed_removed_indexes(before)
    if set(before['tables']) - set(after['tables']):
        raise ValueError('preservation: original table missing')
    if set(after['tables']) - set(before['tables']) - _NEW_TABLES:
        raise ValueError('preservation: unapproved table addition')
    if set(after['indexes']) - set(before['indexes']) - {'uq_multi_source_kline_product'}:
        raise ValueError('preservation: unapproved index addition')
    if set(after['views']) - set(before['views']) - set(rebuilt_views):
        raise ValueError('preservation: unapproved view addition')
    for table in set(after['tables']) - set(before['tables']) - _BOOKKEEPING:
        if after['tables'][table]['rows']:
            raise ValueError('preservation: a new optional table acquired invented facts')
    for table in before['tables']:
        original_constraints = [r for r in before['constraints'] if r[0] == table]
        current_constraints = [r for r in after['constraints'] if r[0] == table]
        if original_constraints != current_constraints:
            raise ValueError('preservation: original constraints changed')
    original_versions = set()
    if 'schema_migration' in before['tables']:
        original_versions = {r[0] for r in con.execute('SELECT version FROM original.main.schema_migration').fetchall()}
    expected_versions = original_versions | set(APPROVED_VERSIONS)
    if applied_versions(con) != expected_versions:
        raise ValueError('preservation: migration ledger acquired unapproved history')
    original_checksums = set()
    if 'schema_migration_checksum' in before['tables']:
        original_checksums = {r[0] for r in con.execute('SELECT version FROM original.main.schema_migration_checksum').fetchall()}
    if {r[0] for r in con.execute('SELECT version FROM schema_migration_checksum').fetchall()} != (
            original_checksums | (set(APPROVED_VERSIONS)-original_versions)):
        raise ValueError('preservation: unapproved migration checksum certification')
    evidence = []
    for table, info in before['tables'].items():
        current = after['tables'][table]
        original_columns = info['columns']
        if current['columns'][:len(original_columns)] != original_columns:
            raise ValueError(f'preservation: original column contract changed ({table})')
        extra = {r[1] for r in current['columns'][len(original_columns):]}
        if extra - _ADDITIONS.get(table, set()):
            raise ValueError(f'preservation: unapproved column addition ({table})')
        if not extra and current['sql'] != info['sql']:
            raise ValueError(f'preservation: original table definition changed ({table})')
        for column in extra:
            if con.execute(f'SELECT count(*) FROM main.{_q(table)} WHERE {_q(column)} IS NOT NULL').fetchone()[0]:
                raise ValueError('preservation: new unknown column was backfilled')
        projection = ','.join(_q(r[1]) for r in original_columns)
        left = f'SELECT {projection} FROM original.main.{_q(table)}'
        right = f'SELECT {projection} FROM main.{_q(table)}'
        if table in _BOOKKEEPING:
            # Compare original rows only; newly approved ledger rows are separate.
            right += f' WHERE version IN (SELECT version FROM original.main.{_q(table)})'
        removed = con.execute(f'SELECT count(*) FROM (({left}) EXCEPT ALL ({right}))').fetchone()[0]
        added = con.execute(f'SELECT count(*) FROM (({right}) EXCEPT ALL ({left}))').fetchone()[0]
        if removed or added:
            raise ValueError(f'preservation: original values/multiplicity changed ({table})')
        evidence.append({'table': table, 'original_columns': [r[1] for r in original_columns],
                         'original_column_count': len(original_columns), 'candidate_column_count': len(current['columns']),
                         'original_rows': info['rows'], 'candidate_rows': current['rows'],
                         'added_columns': sorted(extra), 'source_minus_candidate': removed,
                         'candidate_minus_source': added, 'bookkeeping_original_rows_only': table in _BOOKKEEPING})
    for name, sql in before['indexes'].items():
        if name not in removable_indexes and (after['indexes'].get(name) != sql
                or after['index_owners'].get(name) != before['index_owners'].get(name)
                or after['index_definitions'].get(name) != before['index_definitions'].get(name)):
            raise ValueError('preservation: original index changed')
    for name, sql in before['views'].items():
        if name not in rebuilt_views and after['views'].get(name) != sql:
            raise ValueError('preservation: unrelated original view changed')
    return {'method': 'bidirectional_EXCEPT_ALL_original_columns', 'tables': evidence,
            'original_base_tables': len(before['tables']), 'candidate_base_tables': len(after['tables']),
            'new_tables': sorted(set(after['tables'])-set(before['tables'])),
            'original_schema': before, 'candidate_schema': after,
            'allowed_removed_indexes': sorted(removable_indexes),
            'allowed_removed_index_objects': [dict(name=name, **before['index_owners'][name],
                **before['index_definitions'][name], original_sql=before['indexes'][name])
                for name in sorted(removable_indexes)]}


def _migration_postconditions(con):
    tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
    if not {'qlib_shadow_evaluation', 'multi_source_kline'} <= tables:
        raise ValueError('migration postcondition: required optional schema missing')
    for table, columns in _ADDITIONS.items():
        if table in tables:
            actual = {r[0] for r in con.execute('DESCRIBE '+_q(table)).fetchall()}
            if not columns <= actual:
                raise ValueError('migration postcondition: reviewed columns missing')
    product_index = con.execute("SELECT schema_name,table_name,is_unique,expressions FROM duckdb_indexes() "
        "WHERE database_name=current_database() AND index_name='uq_multi_source_kline_product'").fetchall()
    if (len(product_index) != 1 or product_index[0][:2] != ('main', 'multi_source_kline')
            or _index_definition(product_index[0][2], product_index[0][3]) != _INDEX_DEFINITIONS[_PRODUCT_INDEX]):
        raise ValueError('migration postcondition: exact product unique index missing')
    if con.execute("SELECT count(*) FROM duckdb_indexes() WHERE database_name=current_database() "
                   "AND schema_name='main' AND table_name='multi_source_kline' "
                   "AND index_name IN ('uq_multi_source_kline','ix_multi_source_kline')").fetchone()[0]:
        raise ValueError('migration postcondition: obsolete product index remains')


def _qualification(con, as_of):
    tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
    result = {'as_of': as_of.isoformat(), 'scope': 'retained_historical_rows_not_current_freshness_or_PIT',
              'current_freshness': 'not_certified_historical_rehearsal', 'current_market_ready': False,
              'clock_basis': 'current_Shanghai_receipt_validation_not_historical_arrival_certification',
              'raw_price_identity': {'status': 'not_assessed_missing_schema'}, 'market_counts': {}, 'date_ranges': {}}
    for table, day in [('multi_source_kline', 'source_date'), ('daily_summary', 'date'), ('v_kline_daily', 'trade_date')]:
        if table in tables:
            cols = {r[0] for r in con.execute('DESCRIBE '+_q(table)).fetchall()}
            if day in cols:
                low, high = con.execute(f'SELECT min(TRY_CAST({_q(day)} AS DATE)),max(TRY_CAST({_q(day)} AS DATE)) FROM {_q(table)}').fetchone()
                result['date_ranges'][table] = {'min': str(low) if low else None, 'max': str(high) if high else None,
                    'calendar_age_days': (as_of.date()-high).days if high else None,
                    'not_current_trading_calendar_certification': True}
    if 'multi_source_kline' in tables:
        cols = {r[0] for r in con.execute('DESCRIBE multi_source_kline').fetchall()}
        if {'provider', 'adjustment'} <= cols:
            total, adjustment, provider = con.execute("SELECT count(*),"
                "count(*) FILTER(WHERE lower(trim(coalesce(adjustment,''))) IN ('','unknown')),"
                "count(*) FILTER(WHERE lower(trim(coalesce(provider,''))) IN ('','unknown')) "
                "FROM multi_source_kline").fetchone()
            result['raw_price_identity'] = {'status': 'assessed', 'observed_rows': total,
                'unknown_adjustment_rows': adjustment, 'unknown_provider_rows': provider,
                'unknown_definition': 'NULL_blank_or_literal_unknown_not_inferred_from_other_columns'}
    for table in ('daily_summary', 'v_market_daily', 'v_market_state_inputs'):
        if table not in tables:
            result['market_counts'][table] = {'status': 'not_assessed_missing_schema'}
            continue
        cols = {r[0] for r in con.execute('DESCRIBE '+_q(table)).fetchall()}
        status = market_daily_status_sql(cols, as_of=as_of)
        counts = dict(con.execute(f'SELECT {status} status,count(*) FROM {_q(table)} GROUP BY 1').fetchall())
        total = sum(counts.values())
        result['market_counts'][table] = {'observed_rows': total, 'qualified_rows': counts.get('available', 0),
            'excluded_rows': total-counts.get('available', 0), 'status_counts': counts}
    if 'v_kline_daily' in tables:
        reason = price_observation_reason_sql()
        # Shared qualification plus the same current projection's future clock exclusion.
        reason = f"CASE WHEN fetched_at>CAST({_literal(as_of.isoformat())} AS TIMESTAMP) THEN 'future_source_clock' ELSE ({reason}) END"
        counts = dict(con.execute(f'SELECT {reason} reason,count(*) FROM v_kline_daily GROUP BY 1').fetchall())
        result['canonical_prices'] = {'observed_rows': sum(counts.values()), 'qualified_rows': counts.get(None, 0),
            'excluded_rows': sum(v for k, v in counts.items() if k is not None),
            'excluded_reasons': {k: v for k, v in counts.items() if k is not None}}
    return result


def _fingerprints():
    root = Path(__file__).resolve().parents[2]
    selected = list((root/'trade_system').rglob('*.py'))
    selected += [root/'tools/v2/rehearse_market_migration.py']
    selected += [p for _, p in discover_migrations()]
    return {(str(p.relative_to(root)).replace('\\', '/') if p.is_relative_to(root)
             else 'migrations/'+p.name): _sha256(p) for p in sorted(selected) if p.is_file()}


def _write_receipt(output, receipt):
    temporary = output/'migration-receipt.json.tmp'
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2, default=str)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(output/'migration-receipt.json')


def rehearse(source, output, *, source_sha256):
    """Return a receipt; on failure retain a failed receipt and raise.

    Only reviewed migrations 0014–0017 run. Missing older history is reported,
    never guessed or inserted. The output must be fresh and outside checkouts
    and runtime roots. Callers supply an independently trusted original SHA.
    """
    source, output = _validate_paths(source, output, source_sha256)
    expected = source_sha256.lower()
    with _source_read_guard(source):
        source_bytes = source.stat().st_size
        spill_bytes = min(16*1024**3, max(256*1024**2, source_bytes*4))
        required_free = source_bytes*2 + spill_bytes + 64*1024**2
        available = shutil.disk_usage(output.parent).free
        if available < required_free:
            raise ValueError('insufficient isolated copy and bounded spill capacity')
        if _sha256(source) != expected:
            raise ValueError('source SHA-256 does not match supplied original')
        output.mkdir()
        candidate = output/'migrated-market.duckdb'
        receipt = {'schema': 'market_migration_rehearsal_v1', 'status': 'failed',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'source': {'path': str(source), 'sha256': expected, 'bytes': source.stat().st_size},
            'candidate': {'path': candidate.name}, 'execution_ready': False, 'production_cutover': False,
            'resources': {'threads': 1, 'memory_limit': '1GB', 'max_temp_bytes': spill_bytes,
                'required_free_bytes': required_free, 'observed_free_bytes': available},
            'scope': 'isolated_historical_migrations_0014_0017_not_production_or_current_data',
            'real_accounts': 'not_applicable_current_scope', 'source_code_sql_sha256': _fingerprints()}
        stage, rolled_back, active, con = 'copy', False, False, None
        try:
            # Exclusive creation cannot follow an existing destination link.
            with source.open('rb') as original, candidate.open('xb') as copied:
                shutil.copyfileobj(original, copied, length=8*1024**2)
                copied.flush()
                os.fsync(copied.fileno())
            if source.samefile(candidate) or _sha256(candidate) != expected:
                raise ValueError('source copy is not an independent verified original')
            stage = 'preflight'
            config = {'threads': 1, 'memory_limit': '1GB', 'temp_directory': str(output/'duckdb-temp'),
                      'max_temp_directory_size': f'{spill_bytes}B', 'autoinstall_known_extensions': False,
                      'autoload_known_extensions': False}
            con = duckdb.connect(str(candidate), config=config)
            before = _snapshot(con)
            _check_reserved_index_ownership(before)
            done = sorted(applied_versions(con))
            checksum_exists = 'schema_migration_checksum' in before['tables']
            verified = dict(con.execute('SELECT version,sha256 FROM schema_migration_checksum').fetchall()) if checksum_exists else {}
            receipt['migrations'] = {'approved_versions': list(APPROVED_VERSIONS), 'original_versions': done,
                'unverified_legacy_versions': [v for v in done if v not in verified],
                'original_checksums': {str(v): sha for v, sha in verified.items()},
                'missing_prior_versions': sorted(set(range(1, 14))-set(done)), 'applied_versions': []}
            receipt['product_duplicates'] = _duplicate_products(con)
            if receipt['product_duplicates']['groups']:
                raise ValueError('duplicate product keys require explicit review; no automatic dedupe')
            stage = 'transaction'
            con.execute('BEGIN TRANSACTION')
            active = True
            receipt['migrations']['applied_versions'] = apply_pending_in_transaction(con, versions=APPROVED_VERSIONS)
            _migration_postconditions(con)
            stage = 'normalized_views'
            views = build_normalized_views_on_connection(con)
            stage = 'preservation'
            con.execute(f'ATTACH {_literal(source)} AS original (READ_ONLY)')
            receipt['preservation'] = _preservation(con, before, rebuilt_views=views)
            stage = 'qualification'
            # All contract clocks are Shanghai-naive, independent of the host zone.
            from zoneinfo import ZoneInfo
            as_of = datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
            receipt['qualification'] = _qualification(con, as_of)
            if _sha256(source) != expected or Path(str(source)+'.wal').exists():
                raise ValueError('source changed during rehearsal')
            if receipt['source_code_sql_sha256'] != _fingerprints():
                raise ValueError('source code/SQL changed during rehearsal')
            con.execute('COMMIT')
            active = False
            con.close()
            con = None
            stage = 'reopen_verification'
            with duckdb.connect(str(candidate), read_only=True, config=config) as check:
                _migration_postconditions(check)
                reopened = _snapshot(check)
                previous = receipt['preservation']['candidate_schema']
                # DuckDB serializes new views on commit (e.g. DOUBLE quoting
                # and UNION tree parentheses). Compare stable base contracts,
                # then re-evaluate preservation and qualification on disk.
                if (reopened['tables'] != previous['tables'] or reopened['indexes'] != previous['indexes']
                        or set(reopened['views']) != set(previous['views'])):
                    raise ValueError('candidate schema/count changed after reopening')
                check.execute(f'ATTACH {_literal(source)} AS original (READ_ONLY)')
                receipt['preservation'] = _preservation(check, before, rebuilt_views=views)
                receipt['preservation']['reopened_read_only_verified'] = True
                if _qualification(check, as_of) != receipt['qualification']:
                    raise ValueError('candidate qualification changed after reopening')
            if _sha256(source) != expected:
                raise ValueError('source changed after candidate verification')
            receipt['candidate'].update(sha256=_sha256(candidate), bytes=candidate.stat().st_size)
            receipt['status'] = 'passed'
            receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
            _write_receipt(output, receipt)
            return receipt
        except BaseException as exc:
            if con is not None:
                if active:
                    con.execute('ROLLBACK')
                    rolled_back = True
                con.close()
            # Do not persist exception SQL or raw row payloads from a private DB.
            receipt['status'] = 'failed'
            receipt['failure'] = {'stage': stage, 'type': type(exc).__name__, 'rolled_back': rolled_back,
                                  'detail': 'Candidate rejected; no production or data-qualification approval.'}
            receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
            if candidate.is_file():
                receipt['candidate'].update(sha256=_sha256(candidate), bytes=candidate.stat().st_size)
            _write_receipt(output, receipt)
            raise
