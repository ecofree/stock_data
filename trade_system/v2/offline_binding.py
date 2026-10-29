"""Explicit offline roles; reuse migration preservation and market owners."""
from datetime import datetime
from pathlib import Path
import shutil
import uuid

import duckdb
from trade_system.normalize import normalized_view_names, build_normalized_views_on_connection

from . import market_migration_rehearsal as migration
from .acceptance_scope import SCOPE, account_not_applicable
from .daily_workspace import empty_projection
from .domain import file_hash, identity
from .isolated_workspace import child, read_json, safe_path, write_json
from .recovery_bundle import inventory


def bound_file(folder, record, prefix):
    name = record['path']
    if not name.startswith(prefix+'/'):
        raise ValueError('wrong offline component role')
    path = child(folder, name)
    if file_hash(path) != record['sha256']:
        raise ValueError('offline role file identity differs')
    return path


def normalized_members(value):
    if not isinstance(value,dict) or not value:
        raise ValueError('complete nonempty fixture members required')
    result = {}
    for name, sha in value.items():
        normalized = name.replace('\\','/')
        if normalized in result:
            raise ValueError('ambiguous normalized fixture member')
        result[normalized] = sha
    return result


def verify_observation_fixture(code, workspace, receipt_path):
    """Admit the existing external schema-1 contract through its real owners."""
    from . import research_journal as journal, research_product as product, operator_workflow as workflow
    from .publisher import read_current, read_bundle
    from .research_product_view import receipt_page, plan_receipt_page, review_receipt_page, journal_page
    code, workspace, receipt_path = safe_path(code), safe_path(workspace), safe_path(receipt_path)
    fixture = read_json(receipt_path)
    required = {'schema','fixture_only','label','workspace','pages','note_id','revision_id','plan_id','review_id',
        'D0_snapshot_id','D1_snapshot_id','D2_snapshot_id','fixed_seen_comparison','original_event_hashes_preserved',
        'provider_requests','market_database_reads','execution_ready','production_cutover','acceptance_scope','source_files','artifacts'}
    if not required<=fixture.keys():
        raise ValueError('complete schema-1 fixture fields required')
    if (type(fixture.get('schema')) is not int or fixture['schema']!=1
            or fixture.get('fixture_only') is not True or 'SYNTHETIC' not in fixture.get('label','')
            or fixture.get('execution_ready') is not False or fixture.get('production_cutover') is not False
            or type(fixture.get('provider_requests')) is not int or fixture['provider_requests']!=0
            or type(fixture.get('market_database_reads')) is not int or fixture['market_database_reads']!=0
            or fixture.get('acceptance_scope')!=SCOPE):
        raise ValueError('complete schema-1 synthetic observation provenance required')
    sources = normalized_members(fixture['source_files'])
    names = {p.relative_to(code).as_posix() for p in (code/'trade_system/v2').glob('*.py')}
    if set(sources)!=names:
        raise ValueError('complete direct v2 fixture source membership required')
    for name, sha in sources.items():
        if file_hash(child(code,name))!=sha:
            raise ValueError('observation fixture source bytes differ')
    configuration = read_json(workspace/'workspace-config.json')
    if (configuration.get('acceptance_scope')!=SCOPE or configuration.get('read_only') is not True
            or configuration.get('fixture_only') is not True or configuration.get('provider_requests_allowed') is not False):
        raise ValueError('synthetic fixture workspace configuration differs')
    artifacts = normalized_members(fixture['artifacts'])
    page_names = {f'pages/D{i}-{name}' for i in range(3) for name in ('desk.json','index.html')}
    page_names |= {'pages/'+name for name in ('note-receipt.html','revision-receipt.html',
        'plan-receipt.html','review-receipt.html','selected-chain.html')}
    pages = receipt_path.parent/'pages'
    actual_pages = {'pages/'+p.name for p in pages.iterdir() if p.is_file()}
    if set(artifacts)!=page_names or actual_pages!=page_names or any(p.is_dir() for p in pages.iterdir()):
        raise ValueError('exact retained fixture page membership required')
    for name, sha in artifacts.items():
        if file_hash(child(receipt_path.parent,name))!=sha:
            raise ValueError('retained fixture page hash differs')
    originals = normalized_members(fixture['original_event_hashes_preserved'])
    actual_originals = {p.relative_to(workspace).as_posix():file_hash(safe_path(p))
                        for p in (workspace/'notes').rglob('*.json')}
    if originals!=actual_originals:
        raise ValueError('complete preserved original event/evidence map differs')
    note = journal.read_note(workspace,fixture['note_id'])
    revision = journal.read_note(workspace,fixture['revision_id'])
    plan = workflow.read_observation_plan(workspace,fixture['plan_id'])
    review = journal.read_review(workspace,fixture['review_id'])
    if (revision.get('supersedes')!=note['note_id'] or plan['note_id']!=revision['note_id']
            or review['note_id']!=revision['note_id'] or review.get('plan_id')!=plan['plan_id']
            or note['evidence_id']!=fixture['D0_snapshot_id'] or revision['evidence_id']!=fixture['D0_snapshot_id']
            or review.get('seen_market_snapshot_id')!=fixture['D1_snapshot_id']
            or any('SYNTHETIC' not in value for value in (note['operator'],revision['operator'],plan['operator'],review['reviewer']))):
        raise ValueError('synthetic D0/revision/plan/D1 chain differs')
    fixed = journal.review_projection(workspace,review)
    if identity(fixed['seen_comparison'])!=identity(fixture['fixed_seen_comparison']):
        raise ValueError('fixed seen comparison differs from journal owner')
    current, current_files = read_current(workspace/'publication')
    stages = [read_json(pages/f'D{i}-desk.json')['market'] for i in range(3)]
    for i, market in enumerate(stages):
        if (market['snapshot_id']!=fixture[f'D{i}_snapshot_id']
                or market['snapshot_id']!=identity({k:v for k,v in market.items() if k!='snapshot_id'})
                or market.get('scope')!='synthetic_fixture_not_real_market' or market.get('execution_ready') is not False):
            raise ValueError('retained synthetic market identity differs')
        if i==2:
            bodies = current_files
        else:
            pointer = current.get('published_days',{}).get(market['trade_date'])
            if not pointer:
                raise ValueError('retained fixture publication history missing')
            _, bodies = read_bundle(workspace/'publication',pointer)
        if any((pages/f'D{i}-{name}').read_bytes()!=bodies[name] for name in ('desk.json','index.html')):
            raise ValueError('fixture stage page differs from original publication')
    if not stages[0]['trade_date']<stages[1]['trade_date']<stages[2]['trade_date']:
        raise ValueError('ordered D0/D1/D2 synthetic dates required')
    expected_pages = {'note-receipt.html':receipt_page(note),'revision-receipt.html':receipt_page(revision),
        'plan-receipt.html':plan_receipt_page(plan),'review-receipt.html':review_receipt_page(fixed),
        'selected-chain.html':journal_page(product.journal_navigation(workspace,kind='review',selected=review['review_id']))}
    if any((pages/name).read_text('utf-8')!=body for name,body in expected_pages.items()):
        raise ValueError('fixture receipt/chain page differs from current frozen owner')
    return {'schema':1,'source_members':len(sources),'page_artifacts':len(artifacts),
            'preserved_event_files':len(originals),'chain_verified':True}


def verify_normalized_definitions(source, scratch, *, config):
    """Derive view SQL using the owner over read-through facts, no data copy."""
    scratch = safe_path(scratch)
    if shutil.disk_usage(scratch.parent).free < 16*1024**2:
        raise ValueError('insufficient normalized metadata scratch capacity')
    scratch.mkdir(exist_ok=False)
    configuration = {**config,'temp_directory':str(scratch/'spill')}
    approved = set(normalized_view_names())
    database = scratch/'definitions.duckdb'
    with duckdb.connect(str(database),config=configuration) as con:
        con.execute('ATTACH '+migration._literal(source)+' AS retained (READ_ONLY)')
        objects = con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name='retained' AND NOT internal ORDER BY table_name").fetchall()
        if len(objects)>3000:
            raise ValueError('bounded normalized schema metadata required')
        for (name,) in objects:
            columns = con.execute('DESCRIBE retained.main.'+migration._q(name)).fetchall()
            rowid = '' if any(c[0].lower()=='rowid' for c in columns) else 'rowid AS rowid,'
            con.execute('CREATE VIEW '+migration._q(name)+' AS SELECT '+rowid+'* FROM retained.main.'+migration._q(name))
        other = con.execute("SELECT view_name FROM duckdb_views() WHERE database_name='retained' AND NOT internal ORDER BY view_name").fetchall()
        if len(objects)+len(other)>3000:
            raise ValueError('bounded normalized object metadata required')
        for (name,) in other:
            if name not in approved:
                con.execute('CREATE VIEW '+migration._q(name)+' AS SELECT * FROM retained.main.'+migration._q(name))
        build_normalized_views_on_connection(con)
        facts = con.execute('SELECT count(*) FROM duckdb_tables() WHERE database_name=current_database() AND NOT internal').fetchone()[0]
        if facts:
            raise ValueError('normalized derivation must not copy fact tables')
    # DuckDB canonicalizes CAST type quoting and UNION grouping on persistence.
    # Compare both sides after the same engine close/reopen normalization.
    if database.stat().st_size > 16*1024**2:
        raise ValueError('normalized metadata database budget exceeded')
    with duckdb.connect(str(database),read_only=True,config=configuration) as con:
        con.execute('ATTACH '+migration._literal(source)+' AS retained (READ_ONLY)')
        expected = {name:sql for name,sql in con.execute('SELECT view_name,sql FROM duckdb_views() WHERE database_name=current_database() AND NOT internal').fetchall() if name in approved}
        actual = {name:sql for name,sql in con.execute("SELECT view_name,sql FROM duckdb_views() WHERE database_name='retained' AND NOT internal").fetchall() if name in approved}
        if expected!=actual:
            raise ValueError('normalized view definitions differ from frozen owner')
    result = {'normalized_views_verified':True,'normalized_view_count':len(expected),
        'definitions_sha256':identity(expected),'fact_tables_copied':0,'threads':1,'memory_limit':'1GB',
        'metadata_database_bytes':database.stat().st_size,'metadata_database_budget_bytes':16*1024**2,
        'max_temp_directory_size':config['max_temp_directory_size']}
    write_json(scratch/'derivation.json',result)
    return result


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
    fixture_proof = verify_observation_fixture(code,folder/'workspace',
        bound_file(folder,binding['synthetic_observation_receipt'],'raw_facts'))
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
    scratch.mkdir(exist_ok=True)
    definitions = verify_normalized_definitions(market,scratch/('view-proof-'+uuid.uuid4().hex),config=config)
    projection = binding['projection']
    if datetime.fromisoformat(projection['as_of']).tzinfo is None:
        raise ValueError('aware historical projection clock required')
    rebound = {'schema':'offline_readonly_configuration_v1', 'acceptance_scope':SCOPE,
        'market_database':str(market), 'market_database_sha256':binding['migrated_market_database']['sha256'],
        'raw_archive_database':str(raw), 'raw_archive_sha256':binding['raw_archive_database']['sha256'],
        'projection':binding['projection'], 'read_only':True, 'activation_allowed':False,
        'historical_only':True, 'synthetic_observation_is_separate':True}
    return {'preservation_verified':True, 'qualification_verified':True,
            'normalized_views_verified':True,'normalized_derivation':definitions,'observation_fixture':fixture_proof,
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
