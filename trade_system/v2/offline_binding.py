"""Explicit offline roles; reuse migration preservation and market owners."""
from datetime import datetime
from pathlib import Path

import duckdb
from trade_system.normalize import normalized_view_names

from . import market_migration_rehearsal as migration
from .acceptance_scope import SCOPE, account_not_applicable
from .daily_workspace import empty_projection
from .domain import file_hash, identity
from .isolated_workspace import child, read_json, safe_path
from .recovery_bundle import inventory


def bound_file(folder, record, prefix):
    name = record['path']
    if not name.startswith(prefix+'/'):
        raise ValueError('wrong offline component role')
    path = child(folder, name)
    if file_hash(path) != record['sha256']:
        raise ValueError('offline role file identity differs')
    return path


def verify_bindings(folder, binding):
    folder = Path(folder).resolve(strict=True)
    if binding.get('schema') != 'offline_database_bindings_v1' or binding.get('execution_ready') is not False:
        raise ValueError('explicit offline database bindings required')
    raw = bound_file(folder, binding['raw_archive_database'], 'databases')
    market = bound_file(folder, binding['migrated_market_database'], 'databases')
    if raw == market or binding['raw_archive_database']['sha256'] == binding['migrated_market_database']['sha256']:
        raise ValueError('distinct raw archive and migrated market roles required')
    database_files = inventory(folder/'databases')
    for role in ('raw_archive_database', 'migrated_market_database'):
        if sum(r['sha256'] == binding[role]['sha256'] for r in database_files.values()) != 1:
            raise ValueError('ambiguous offline database role')
    receipt = read_json(bound_file(folder, binding['migration_receipt'], 'raw_facts'))
    if (receipt.get('schema') != 'market_migration_rehearsal_v1' or receipt.get('status') != 'passed'
            or receipt.get('scope') != 'isolated_historical_migrations_0014_0017_not_production_or_current_data'
            or receipt.get('execution_ready') is not False or receipt.get('production_cutover') is not False
            or receipt.get('real_accounts') != 'not_applicable_current_scope'
            or receipt['source']['sha256'] != binding['raw_archive_database']['sha256']
            or receipt['candidate']['sha256'] != binding['migrated_market_database']['sha256']
            or receipt['source']['bytes'] != raw.stat().st_size
            or receipt['candidate']['bytes'] != market.stat().st_size
            or identity(receipt['qualification']) != binding['qualification_sha256']
            or receipt['preservation'].get('reopened_read_only_verified') is not True):
        raise ValueError('migration receipt does not prove these database roles')
    code = folder/'code'
    expected = receipt['source_code_sql_sha256']
    actual_names = {p.relative_to(code).as_posix() for p in (code/'trade_system').rglob('*.py')}
    actual_names |= {'tools/v2/rehearse_market_migration.py'}
    actual_names |= {p.relative_to(code).as_posix() for p in (code/'migrations').glob('[0-9][0-9][0-9][0-9]_*.sql')}
    if set(expected) != actual_names:
        raise ValueError('migration source export membership differs')
    for name, sha in expected.items():
        if file_hash(child(code, name)) != sha:
            raise ValueError('migration source export bytes differ')
    fixture = read_json(bound_file(folder, binding['synthetic_observation_receipt'], 'raw_facts'))
    if (fixture.get('fixture_only') is not True or 'SYNTHETIC' not in fixture.get('label', '')
            or fixture.get('execution_ready') is not False or fixture.get('acceptance_scope') != SCOPE
            or fixture.get('provider_requests') != 0 or fixture.get('market_database_reads') != 0):
        raise ValueError('explicit synthetic observation provenance required')
    for name, sha in fixture['source_files'].items():
        if file_hash(child(code, name.replace('\\', '/'))) != sha:
            raise ValueError('observation fixture source bytes differ')
    if inventory(folder/'workspace') != binding['observation_workspace_files']:
        raise ValueError('synthetic observation workspace differs')
    scratch = safe_path(folder.parent/(folder.name+'-binding-temp'))
    spill = min(16*1024**3,max(256*1024**2,raw.stat().st_size*4))
    config = {'threads':1, 'memory_limit':'1GB', 'temp_directory':str(scratch),
              'max_temp_directory_size':str(spill)+'B',
              'autoinstall_known_extensions':False, 'autoload_known_extensions':False}
    with duckdb.connect(str(raw), read_only=True, config=config) as original:
        before = migration._snapshot(original)
    with duckdb.connect(str(market), read_only=True, config=config) as con:
        con.execute('BEGIN')
        migration._migration_postconditions(con)
        con.execute('ATTACH '+migration._literal(raw)+' AS original (READ_ONLY)')
        preserved = migration._preservation(con, before,
            rebuilt_views=normalized_view_names())
        preserved['reopened_read_only_verified'] = True
        if identity(preserved) != identity(receipt['preservation']):
            raise ValueError('independent migration preservation differs')
        qualification = migration._qualification(con, datetime.fromisoformat(receipt['qualification']['as_of']))
        if identity(qualification) != binding['qualification_sha256']:
            raise ValueError('independent qualification differs')
    projection = binding['projection']
    if datetime.fromisoformat(projection['as_of']).tzinfo is None:
        raise ValueError('aware historical projection clock required')
    rebound = {'schema':'offline_readonly_configuration_v1', 'acceptance_scope':SCOPE,
        'market_database':str(market), 'market_database_sha256':binding['migrated_market_database']['sha256'],
        'raw_archive_database':str(raw), 'raw_archive_sha256':binding['raw_archive_database']['sha256'],
        'projection':binding['projection'], 'read_only':True, 'activation_allowed':False,
        'historical_only':True, 'synthetic_observation_is_separate':True}
    return {'preservation_verified':True, 'qualification_verified':True,
            'qualification_sha256':binding['qualification_sha256'],
            'rebound_readonly_configuration':rebound}


def historical_projection(configuration):
    """Consume the derived binding, never original workspace configuration."""
    from .market_workspace import project
    config = read_json(configuration)
    if (config.get('schema') != 'offline_readonly_configuration_v1' or config.get('read_only') is not True
            or config.get('activation_allowed') is not False or config.get('historical_only') is not True):
        raise ValueError('non-activating historical binding required')
    source = Path(config['market_database'])
    if file_hash(source) != config['market_database_sha256']:
        raise ValueError('bound historical market database changed')
    with duckdb.connect(str(source), read_only=True, config={'threads':1,'memory_limit':'512MB',
            'temp_directory':str(safe_path(Path(configuration).parent/'historical-market-temp')),
            'max_temp_directory_size':'1GB',
            'autoinstall_known_extensions':False, 'autoload_known_extensions':False}) as con:
        con.execute('BEGIN')
        market = project(con, config['projection']['day'], config['projection']['as_of'], set())
    data = empty_projection()
    data.update(market=market, acceptance_scope=SCOPE, historical_rehearsal=True,
                plans={'account':account_not_applicable(),'rows':[]},
                offline_material_scope='historical_market_not_current_or_QLib_environment')
    return data
