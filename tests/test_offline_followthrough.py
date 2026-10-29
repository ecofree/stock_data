"""Portable historical recovery boundaries, with disposable small databases."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil

import duckdb
import pytest

from test_historical_migration_rehearsal import source_db, rehearsal
from test_isolated_capabilities import prepared
from trade_system.v2 import recovery_bundle, capacity_acceptance
from trade_system.v2.acceptance_scope import SCOPE
from trade_system.v2.domain import identity, file_hash
from trade_system.v2.isolated_workspace import write_json, read_json


def complete_observation_fixture(output, source):
    """Test-only schema-1 fixture using real journal/publisher/render owners."""
    from datetime import timedelta
    from trade_system.v2 import research_product as product, research_journal as journal
    from trade_system.v2 import operator_workflow as workflow
    from trade_system.v2.domain import now_utc
    from trade_system.v2.publisher import read_current
    from trade_system.v2.research_product_view import receipt_page, plan_receipt_page, review_receipt_page, journal_page
    workspace, pages = output/'workspace', output/'pages'
    workspace.mkdir(parents=True); pages.mkdir()
    write_json(workspace/'workspace-config.json',{'acceptance_scope':SCOPE,'read_only':True,
        'fixture_only':True,'provider_requests_allowed':False})
    snapshots = []
    for day, close in [('2026-10-12',10),('2026-10-13',11),('2026-10-14',12)]:
        row = {'stock_code':'000001','stock_name':'SYNTHETIC','close':close,'change_pct':1,
            'price_status':'available','price_reason':None,'price_contract':['xiaodefa','none','shares','yuan']}
        market = {'trade_date':day,'as_of':day+'T18:00:00+08:00','stocks':{'000001':row},
            'themes':[],'breadth':{'rise':1,'fall':0},'regime':'SYNTHETIC','session_state':'calendar_unknown',
            'scope':'synthetic_fixture_not_real_market','execution_ready':False}
        snapshots.append(dict(market,snapshot_id=identity(market)))
    def retain(label):
        _, artifacts = read_current(workspace/'publication')
        for name in ('desk.json','index.html'):
            (pages/(label+'-'+name)).write_bytes(artifacts[name])
    product.publish_desk(workspace,market=snapshots[0])
    command = dict(instrument='000001',operator='SYNTHETIC ONLY',intent='observe',hypothesis='D0',
        invalidation='Synthetic condition',evidence_id=snapshots[0]['snapshot_id'],request_id='1'*32)
    note = product.save_note(workspace,command)
    revision = product.save_note(workspace,dict(command,supersedes=note,request_id='2'*32,hypothesis='D0 revision'))
    plan = workflow.save_observation_plan(workspace,dict(note_id=revision,operator='SYNTHETIC ONLY',condition='manual',
        threshold='',valid_until=(now_utc()+timedelta(days=7)).isoformat(),request_id='3'*32))
    product.publish_desk(workspace,market=snapshots[0]); retain('D0')
    product.publish_desk(workspace,market=snapshots[1])
    review = journal.save_review(workspace,dict(note_id=revision,plan_id=plan,request_id='4'*32,reviewer='SYNTHETIC ONLY',
        conclusion='not_triggered',evidence='D1 synthetic',seen_market_snapshot_id=snapshots[1]['snapshot_id'],
        seen_market_evidence_file_id=identity(snapshots[1])))
    product.publish_desk(workspace,market=snapshots[1]); retain('D1')
    original = {p.relative_to(workspace).as_posix():file_hash(p) for p in (workspace/'notes').rglob('*.json')}
    product.publish_desk(workspace,market=snapshots[2]); retain('D2')
    bound = journal.review_projection(workspace,journal.read_review(workspace,review))
    documents = {'note-receipt.html':receipt_page(journal.read_note(workspace,note)),
        'revision-receipt.html':receipt_page(journal.read_note(workspace,revision)),
        'plan-receipt.html':plan_receipt_page(workflow.read_observation_plan(workspace,plan)),
        'review-receipt.html':review_receipt_page(bound),
        'selected-chain.html':journal_page(product.journal_navigation(workspace,kind='review',selected=review))}
    for name, body in documents.items():
        (pages/name).write_text(body,encoding='utf-8')
    receipt = {'schema':1,'fixture_only':True,'label':'SYNTHETIC D0 -> revision -> plan -> D1 -> D2',
        'workspace':str(workspace),'pages':str(pages),'note_id':note,'revision_id':revision,'plan_id':plan,'review_id':review,
        **{f'D{i}_snapshot_id':market['snapshot_id'] for i,market in enumerate(snapshots)},
        'fixed_seen_comparison':bound['seen_comparison'],'original_event_hashes_preserved':original,
        'provider_requests':0,'market_database_reads':0,'execution_ready':False,'production_cutover':False,
        'acceptance_scope':SCOPE,'source_files':{p.relative_to(source).as_posix():file_hash(p)
            for p in (source/'trade_system/v2').glob('*.py')},
        'artifacts':{p.relative_to(output).as_posix():file_hash(p) for p in pages.iterdir()}}
    write_json(output/'fixture-receipt.json',receipt)
    return receipt


@pytest.fixture
def bound_lab(tmp_path):
    root = tmp_path/'lab'
    plan = prepared(root)
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open INTEGER)')
        con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2026-09-04',1),('SZSE','2026-09-04',1)")
    result = rehearsal(source, tmp_path/'migration')
    dbs = root/'inputs/databases'
    (dbs/'market.duckdb').unlink()  # Owned synthetic fixture only.
    shutil.copyfile(source, dbs/'raw.duckdb')
    shutil.copyfile(tmp_path/'migration/migrated-market.duckdb', dbs/'migrated.duckdb')
    facts = root/'inputs/raw_facts'
    shutil.copyfile(tmp_path/'migration/migration-receipt.json', facts/'migration.json')
    # Export exactly the source contract the real migration receipt binds.
    code = root/'inputs/code'
    actual = Path(__file__).resolve().parents[1]
    for name in result['source_code_sql_sha256']:
        dest = code/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(actual/name, dest)
    from trade_system.v2.recovery_bundle import inventory
    fixture = complete_observation_fixture(tmp_path/'complete-fixture',actual)
    (root/'inputs/workspace').rename(facts/'prepared-workspace')
    shutil.copytree(tmp_path/'complete-fixture/workspace',root/'inputs/workspace')
    shutil.copytree(tmp_path/'complete-fixture/pages',facts/'pages')
    write_json(facts/'fixture.json', fixture)
    binding = {'schema':'offline_database_bindings_v1',
        'raw_archive_database':{'path':'databases/raw.duckdb', 'sha256':file_hash(dbs/'raw.duckdb')},
        'migrated_market_database':{'path':'databases/migrated.duckdb', 'sha256':file_hash(dbs/'migrated.duckdb')},
        'migration_receipt':{'path':'raw_facts/migration.json', 'sha256':file_hash(facts/'migration.json')},
        'qualification_sha256':identity(result['qualification']),
        'synthetic_observation_receipt':{'path':'raw_facts/fixture.json', 'sha256':file_hash(facts/'fixture.json')},
        'observation_workspace_files':inventory(root/'inputs/workspace'),
        'projection':{'day':'2026-09-04', 'as_of':'2026-10-29T18:00:00+08:00'},
        'execution_ready':False}
    write_json(facts/'offline-bindings.json', binding)
    recovery_bundle.capture(root, plan, 'bundles/initial')
    recovery_bundle.restore(root, 'bundles/initial', 'restores/first')
    capacity_acceptance.archive_table(root, 'inputs/databases/raw.duckdb', 'multi_source_kline',
                                     'restores/first', 'capacity/prices')
    return root, binding


def test_binding_rechecks_preservation_and_projection_uses_migrated_database(bound_lab):
    from trade_system.v2.offline_binding import verify_bindings, historical_projection
    root, binding = bound_lab
    folder = root/'restores/first'
    before = recovery_bundle.inventory(folder)
    verified = verify_bindings(folder, binding)
    assert verified['preservation_verified'] and verified['qualification_verified']
    config = root/'rebound.json'
    write_json(config, verified['rebound_readonly_configuration'])
    data = historical_projection(config)
    assert data['market']['trade_date'] == '2026-09-04'
    assert data['market']['stocks']['000001']['close'] == 10
    assert data['market']['stocks']['000001']['price_reason'] == 'unknown_adjustment'
    assert data['historical_rehearsal'] and data['execution_ready'] is False
    assert recovery_bundle.inventory(folder) == before


@pytest.mark.parametrize('damage', ['swapped', 'same-role', 'missing', 'receipt', 'wrong-source', 'source-code', 'path'])
def test_database_role_damage_is_rejected(bound_lab, damage):
    from trade_system.v2.offline_binding import verify_bindings
    root, original = bound_lab
    binding = deepcopy(original)
    folder = root/'restores/first'
    if damage == 'swapped':
        binding['raw_archive_database'], binding['migrated_market_database'] = binding['migrated_market_database'], binding['raw_archive_database']
    elif damage == 'same-role':
        binding['migrated_market_database'] = binding['raw_archive_database']
    elif damage == 'missing':
        (folder/'databases/migrated.duckdb').unlink()
    elif damage == 'receipt':
        (folder/'raw_facts/migration.json').write_text('{}')
    elif damage == 'wrong-source':
        receipt = read_json(folder/'raw_facts/migration.json')
        receipt['source']['sha256'] = '0'*64
        (folder/'raw_facts/migration.json').write_text(json.dumps(receipt))
        binding['migration_receipt']['sha256'] = file_hash(folder/'raw_facts/migration.json')
    elif damage == 'source-code':
        (folder/'code/trade_system/v2/domain.py').write_text('# changed')
    else:
        binding['raw_archive_database']['path'] = '../escape.duckdb'
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_bindings(folder, binding)


def test_binding_independently_detects_resealed_changed_business_values(bound_lab):
    from trade_system.v2.offline_binding import verify_bindings
    root, binding = bound_lab
    folder = root/'restores/first'
    market = folder/'databases/migrated.duckdb'
    with duckdb.connect(str(market)) as con:
        con.execute('UPDATE multi_source_kline SET close=99')
    receipt = read_json(folder/'raw_facts/migration.json')
    receipt['candidate']['sha256'] = file_hash(market)
    receipt['candidate']['bytes'] = market.stat().st_size
    (folder/'raw_facts/migration.json').write_text(json.dumps(receipt))
    binding['migrated_market_database']['sha256'] = file_hash(market)
    binding['migration_receipt']['sha256'] = file_hash(folder/'raw_facts/migration.json')
    with pytest.raises(ValueError,match='values|preservation'):
        verify_bindings(folder,binding)


def test_binding_preservation_uses_bounded_scratch_outside_restored_files(bound_lab,monkeypatch):
    from trade_system.v2 import offline_binding
    root, binding = bound_lab
    folder = root/'restores/first'
    previous = offline_binding.migration._preservation
    def inspect(con,*args,**kwargs):
        scratch = Path(con.execute("SELECT current_setting('temp_directory')").fetchone()[0]).resolve()
        assert scratch == folder.parent/(folder.name+'-binding-temp')
        assert con.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0] == '256.0 MiB'
        assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 1
        return previous(con,*args,**kwargs)
    monkeypatch.setattr(offline_binding.migration,'_preservation',inspect)
    offline_binding.verify_bindings(folder,binding)


def test_receipt_cannot_expand_normalized_view_allowlist(bound_lab):
    from trade_system.v2.offline_binding import verify_bindings, migration
    root, binding = bound_lab
    folder = root/'restores/first'
    market = folder/'databases/migrated.duckdb'
    with duckdb.connect(str(market)) as con:
        con.execute('CREATE VIEW invented_offline_view AS SELECT 42 AS invented')
    with duckdb.connect(str(market),read_only=True) as con:
        snapshot = migration._snapshot(con)
    receipt = read_json(folder/'raw_facts/migration.json')
    receipt['candidate'].update(sha256=file_hash(market),bytes=market.stat().st_size)
    receipt['preservation']['candidate_schema'] = snapshot
    (folder/'raw_facts/migration.json').write_text(json.dumps(receipt))
    binding['migrated_market_database']['sha256'] = file_hash(market)
    binding['migration_receipt']['sha256'] = file_hash(folder/'raw_facts/migration.json')
    with pytest.raises(ValueError,match='unapproved view'):
        verify_bindings(folder,binding)


def test_approved_view_definition_cannot_change_close_ten_to_990(bound_lab):
    from trade_system.v2.offline_binding import verify_bindings, migration
    root, binding = bound_lab
    folder = root/'restores/first'; market = folder/'databases/migrated.duckdb'
    before_raw = file_hash(folder/'databases/raw.duckdb')
    # Retain the independently reproduced false-value SQL without introducing
    # an extra unapproved view that would mask the definition loophole.
    with duckdb.connect(str(market)) as con:
        con.execute("CREATE OR REPLACE VIEW v_kline_daily AS SELECT CAST(source_date AS VARCHAR) trade_date,asset_code stock_code,open,high,low,close*99 AS close,CAST(volume AS BIGINT) volume,CAST(amount AS BIGINT) turnover,change_pct,'D' ktype,'multi_source_kline' source_table,false is_fallback,fetched_at,'shares' volume_unit,'yuan' amount_unit,coalesce(adjustment,'unknown') adjustment,provider FROM multi_source_kline")
    with duckdb.connect(str(market),read_only=True) as con:
        snapshot = migration._snapshot(con)
        assert con.execute('SELECT close FROM multi_source_kline').fetchone()[0] == 10
        assert con.execute('SELECT close FROM v_kline_daily').fetchone()[0] == 990
    receipt = read_json(folder/'raw_facts/migration.json')
    receipt['candidate'].update(sha256=file_hash(market),bytes=market.stat().st_size)
    receipt['preservation']['candidate_schema'] = snapshot
    (folder/'raw_facts/migration.json').write_text(json.dumps(receipt))
    binding['migrated_market_database']['sha256'] = file_hash(market)
    binding['migration_receipt']['sha256'] = file_hash(folder/'raw_facts/migration.json')
    before_market = file_hash(market)
    with pytest.raises(ValueError,match='normalized view'):
        verify_bindings(folder,binding)
    assert file_hash(market) == before_market and file_hash(folder/'databases/raw.duckdb') == before_raw


@pytest.mark.parametrize('damage',['empty-source','partial-source','duplicate-source','empty-artifacts','partial-artifacts',
    'artifact-hash','fake-id','fake-parent','empty-events','partial-events','comparison','schema'])
def test_complete_fixture_contract_rejects_partial_or_inconsistent_receipts(bound_lab,damage):
    from trade_system.v2.offline_binding import verify_bindings
    root, binding = bound_lab; folder = root/'restores/first'; path = folder/'raw_facts/fixture.json'
    fixture = read_json(path)
    if damage == 'empty-source': fixture['source_files'] = {}
    elif damage == 'partial-source': fixture['source_files'].pop(next(iter(fixture['source_files'])))
    elif damage == 'duplicate-source':
        name = next(iter(fixture['source_files'])); fixture['source_files'][name.replace('/','\\')] = fixture['source_files'][name]
    elif damage == 'empty-artifacts': fixture['artifacts'] = {}
    elif damage == 'partial-artifacts': fixture['artifacts'].pop('pages/D1-desk.json')
    elif damage == 'artifact-hash': (folder/'raw_facts/pages/D1-index.html').write_text('changed')
    elif damage == 'fake-id': fixture['review_id'] = 'f'*64
    elif damage == 'fake-parent': fixture['revision_id'] = fixture['note_id']
    elif damage == 'empty-events': fixture['original_event_hashes_preserved'] = {}
    elif damage == 'partial-events': fixture['original_event_hashes_preserved'].pop(next(iter(fixture['original_event_hashes_preserved'])))
    elif damage == 'schema': fixture['schema'] = True
    else: fixture['fixed_seen_comparison']['current_close'] = 990
    path.write_text(json.dumps(fixture)); binding['synthetic_observation_receipt']['sha256'] = file_hash(path)
    with pytest.raises((ValueError,FileNotFoundError)):
        verify_bindings(folder,binding)


def test_complete_fixture_windows_members_are_portable_and_read_only(bound_lab):
    from trade_system.v2.offline_binding import verify_bindings
    root, binding = bound_lab; folder = root/'restores/first'; path = folder/'raw_facts/fixture.json'
    fixture = read_json(path)
    for key in ('source_files','artifacts','original_event_hashes_preserved'):
        fixture[key] = {name.replace('/','\\'):sha for name,sha in fixture[key].items()}
    path.write_text(json.dumps(fixture)); binding['synthetic_observation_receipt']['sha256'] = file_hash(path)
    before = recovery_bundle.inventory(folder)
    proof = verify_bindings(folder,binding)
    assert proof['observation_fixture']['page_artifacts'] == 11
    assert proof['observation_fixture']['chain_verified']
    assert proof['normalized_derivation']['fact_tables_copied'] == 0
    assert proof['normalized_derivation']['metadata_database_bytes'] <= 16*1024**2
    assert recovery_bundle.inventory(folder) == before


def test_approved_view_cannot_be_missing_from_resealed_candidate(bound_lab):
    from trade_system.v2.offline_binding import verify_bindings, migration
    root, binding = bound_lab; folder = root/'restores/first'; market = folder/'databases/migrated.duckdb'
    with duckdb.connect(str(market)) as con: con.execute('DROP VIEW v_data_coverage')
    with duckdb.connect(str(market),read_only=True) as con: snapshot = migration._snapshot(con)
    receipt = read_json(folder/'raw_facts/migration.json')
    receipt['candidate'].update(sha256=file_hash(market),bytes=market.stat().st_size)
    receipt['preservation']['candidate_schema'] = snapshot
    (folder/'raw_facts/migration.json').write_text(json.dumps(receipt))
    binding['migrated_market_database']['sha256'] = file_hash(market)
    binding['migration_receipt']['sha256'] = file_hash(folder/'raw_facts/migration.json')
    with pytest.raises(ValueError,match='normalized view'):
        verify_bindings(folder,binding)


def test_attached_catalog_cannot_supply_current_table_or_coverage(tmp_path):
    from trade_system.quality import table_exists, table_columns
    from trade_system.normalize import build_normalized_views_on_connection
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE local_facts(local_value INTEGER)')
        con.execute("ATTACH ':memory:' AS foreign_catalog")
        con.execute('CREATE TABLE foreign_catalog.only_foreign(secret VARCHAR)')
        con.execute('CREATE TABLE foreign_catalog.local_facts(foreign_value VARCHAR)')
        assert not table_exists(con,'only_foreign')
        assert table_columns(con,'only_foreign') == []
        assert table_columns(con,'local_facts') == ['local_value']
        build_normalized_views_on_connection(con)
        assert con.execute('SELECT table_name FROM v_data_coverage ORDER BY table_name').fetchall() == [('local_facts',)]


def test_attached_table_names_do_not_duplicate_current_coverage():
    from trade_system.normalize import build_normalized_views_on_connection
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE facts(value INTEGER)'); con.execute('INSERT INTO facts VALUES (1),(2)')
        build_normalized_views_on_connection(con)
        assert con.execute('SELECT * FROM v_data_coverage').fetchall() == [('facts',2)]
        con.execute("ATTACH ':memory:' AS foreign_catalog")
        con.execute('CREATE TABLE foreign_catalog.facts(foreign_value VARCHAR)')
        con.execute("INSERT INTO foreign_catalog.facts VALUES ('other')")
        build_normalized_views_on_connection(con)
        assert con.execute('SELECT * FROM v_data_coverage').fetchall() == [('facts',2)]


def test_workspace_staging_uses_authority_inventory_without_weakening_stale_reader(tmp_path):
    from tools.v2 import build_offline_candidate as builder
    from trade_system.v2 import journal_index as index
    actual = Path(__file__).resolve().parents[1]
    complete_observation_fixture(tmp_path/'fixture',actual)
    source = tmp_path/'fixture/workspace'
    assert index.cache_path(source).is_file() and index.recent(source,'note')[1] == 2
    before = {p.relative_to(source).as_posix():file_hash(p) for p in source.rglob('*') if p.is_file()}
    expected = recovery_bundle.inventory(source)
    builder.stage_workspace(source,tmp_path/'stage')
    assert recovery_bundle.inventory(tmp_path/'stage') == expected
    assert not index.cache_path(tmp_path/'stage').exists()
    assert not list((tmp_path/'stage').rglob('*.guard'))
    assert index.recent(tmp_path/'stage','note')[1] == 2
    assert before == {p.relative_to(source).as_posix():file_hash(p) for p in source.rglob('*') if p.is_file()}
    shutil.copytree(source,tmp_path/'stale-cache-copy')
    with pytest.raises(RuntimeError,match='stale'):
        index.recent(tmp_path/'stale-cache-copy','note')


def test_definition_scratch_preserves_legacy_rowid_tie_and_copies_no_facts(tmp_path):
    from trade_system.v2.offline_binding import verify_normalized_definitions
    from trade_system.normalize import build_normalized_views_on_connection
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('DELETE FROM multi_source_kline')
        con.execute('CREATE TABLE kline(date DATE,stock_code VARCHAR,close DOUBLE,ktype VARCHAR)')
        con.execute("INSERT INTO kline VALUES ('2026-09-04','000001',10,'D'),('2026-09-04','000001',20,'D')")
        build_normalized_views_on_connection(con)
    # This legacy rowid test exercises the normalize owner only. Its dynamic
    # current_timestamp fallback is deliberately not a migration qualification.
    market = source; before = file_hash(market)
    config = {'threads':1,'memory_limit':'1GB','max_temp_directory_size':'268435456B',
              'autoinstall_known_extensions':False,'autoload_known_extensions':False}
    result = verify_normalized_definitions(market,tmp_path/'metadata',config=config)
    assert result['fact_tables_copied'] == 0 and result['metadata_database_bytes'] <= 16*1024**2
    with duckdb.connect(str(tmp_path/'metadata/definitions.duckdb'),read_only=True) as con:
        con.execute("ATTACH '"+str(market).replace("'","''")+"' AS retained (READ_ONLY)")
        assert con.execute('SELECT close FROM v_kline_daily').fetchall() == [(20.0,)]
        assert con.execute('SELECT count(*) FROM duckdb_tables() WHERE database_name=current_database() AND NOT internal').fetchone()[0] == 0
    assert file_hash(market) == before


def test_full_schema_all_nineteen_owner_views_survive_readonly_derivation(tmp_path):
    from trade_system.schema import init_schema
    from trade_system.normalize import build_normalized_views_on_connection
    from trade_system.v2.offline_binding import verify_normalized_definitions
    market = tmp_path/'full-empty-schema.duckdb'
    with duckdb.connect(str(market)) as con:
        init_schema(con)
        assert len(build_normalized_views_on_connection(con)) == 19
    before = file_hash(market)
    result = verify_normalized_definitions(market,tmp_path/'full-schema-proof',config={
        'threads':1,'memory_limit':'1GB','max_temp_directory_size':'268435456B',
        'autoinstall_known_extensions':False,'autoload_known_extensions':False})
    assert result['normalized_view_count'] == 19 and result['fact_tables_copied'] == 0
    assert file_hash(market) == before


def test_verifier_exports_separate_historical_and_synthetic_pages(bound_lab, tmp_path):
    from tools.v2.verify_offline_recovery import verify
    root, _ = bound_lab
    before = recovery_bundle.inventory(root/'restores/first/workspace')
    output = tmp_path/'evidence'
    output.mkdir()
    result = verify(root, 'restores/first', 'capacity/prices', output/'business.json',
                    bindings='raw_facts/offline-bindings.json')
    assert result['verified']
    market = read_json(output/'historical-market.json')
    assert market['market']['trade_date'] == '2026-09-04'
    assert market['market']['stocks']['000001']['close'] == 10
    assert 'SYNTHETIC' in (output/'workbench.html').read_text('utf-8')
    assert '历史存量' in (output/'historical-market.html').read_text('utf-8')
    assert 'historical-market.html' in (output/'index.html').read_text('utf-8')
    assert before == recovery_bundle.inventory(root/'restores/first/workspace')


def targets():
    return {'schema':'stockdata_recovery_targets_v1', 'scope':SCOPE,
        'rpo':{'unit':'trading_session','maximum':1,'seconds_equivalent':None},
        'rto':{'maximum_seconds':14400,'start':'begin_retrieval_or_copy_for_the_recovery_attempt',
               'end':'technical_verification_complete_and_user_confirms_pages_and_research_materials_usable'},
        'production_activation_authorized':False,'real_accounts':'not_applicable_current_scope'}


def test_home_collection_keeps_human_and_independent_machine_pending(tmp_path):
    from tools.v2.offline_home import collect
    dest, receipt, _ = technical_receipt(tmp_path)
    evidence = dest/'evidence'
    receipt['intake_started_at'] = None
    (evidence/'completed.json').write_text(json.dumps(receipt))
    (dest/'private.duckdb').write_text('PRIVATE DATABASE')
    (evidence/'unknown.log').write_text('TOKEN=must-not-collect')
    result = collect(dest, tmp_path/'collected', 'a'*64)
    assert result['rto_assessment'] == 'pending_intake_start_and_human_confirmation'
    assert result['rpo']['unit'] == 'trading_session' and result['rpo']['seconds_equivalent'] is None
    assert result['ongoing_rpo_assessment'] == 'not_certified_historical_snapshot'
    assert result['offsite_accepted'] is False
    assert result['independent_machine_status'] == 'pending_external_verification'
    text = (tmp_path/'collected/acceptance.json').read_text('utf-8')
    assert 'PRIVATE' not in text and 'must-not-collect' not in text
    assert {p.name for p in (tmp_path/'collected').iterdir()} == {'acceptance.json','human-checklist.json'}


def test_source_export_rejects_changed_missing_or_unsafe_files(tmp_path):
    from tools.v2.build_offline_candidate import export_source
    source = tmp_path/'source'; source.mkdir(); (source/'safe.py').write_text('pass\n')
    manifest = {'safe.py':file_hash(source/'safe.py')}
    (source/'unlisted-private.txt').write_text('private')
    export_source(source, manifest, tmp_path/'good')
    assert not (tmp_path/'good/unlisted-private.txt').exists()
    (source/'safe.py').write_text('changed')
    for files in (manifest, {'missing.py':'0'*64}, {'../safe.py':'0'*64}, {'.env':'0'*64}):
        with pytest.raises((ValueError,FileNotFoundError)):
            export_source(source, files, tmp_path/('bad-'+str(len(list(tmp_path.iterdir())))))


def test_v2_material_closure_does_not_require_v3_tools(tmp_path):
    from test_offline_acceptance import fake_package
    from tools.v2.offline_package import verify_package
    root, pin = fake_package(tmp_path)
    assert not (root/'code/tools/v2/offline_home.py').exists()
    result = verify_package(root, pin)
    assert result['schema'] == 'stockdata_observation_offline_package_v2'


def test_windows_preflight_and_no_overwrite_probe_are_local_and_bounded(tmp_path):
    from tools.v2.offline_home import platform_preflight, filesystem_probe
    result = platform_preflight(tmp_path/'unused', 1)
    assert result['system'] == 'Windows' and result['filesystem'] == 'NTFS'
    assert len(result['machine_fingerprint']) == 64
    assert not (tmp_path/'unused').exists()
    with pytest.raises(ValueError, match='capacity'):
        platform_preflight(tmp_path/'unused', 10**15)
    assert filesystem_probe(tmp_path)['no_overwrite']


def technical_receipt(tmp_path):
    from tools.v2.offline_home import ARTIFACTS
    dest = tmp_path/'technical'; (dest/'evidence').mkdir(parents=True)
    artifacts = {}
    platform = {'machine_fingerprint':'b'*64,'system':'Windows','architecture':'AMD64',
                'version':[10,0,26100],'filesystem':'NTFS','support_status':'candidate_not_cross_version_certification'}
    payloads = {
        'environment-evidence/verification.json':{'passed':True,'installed_site_packages_copied':False,
            'production_or_global_python_changed':False},
        'evidence/platform.json':platform,
        'evidence/business-verification.json':{'acceptance_scope':SCOPE,'execution_ready':False,
            'real_accounts':'not_applicable_current_scope',
            'recovery':{'files':{'databases/raw.duckdb':{'sha256':'d'*64}},'offsite_accepted':False},
            'capacity':{'complete_multiset_verified':True,'rows':1,'batches':1},
            'database_roles':{'preservation_verified':True,'qualification_verified':True,'normalized_views_verified':True},
            'rebound_readonly_configuration':{'read_only':True,'activation_allowed':False}},
        'evidence/historical-market.json':{'historical_rehearsal':True,
            'market':{'trade_date':'2026-09-04','snapshot_id':'c'*64}},
    }
    for name in ARTIFACTS:
        path = dest/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(payloads.get(name,{})))
        artifacts[name] = file_hash(path)
    now = datetime.now(timezone.utc)
    receipt = {'schema':'offline_package_local_recovery_v1','package_sha256':'a'*64,
        'technical_started_at':(now-timedelta(minutes=10)).isoformat(),
        'technical_finished_at':(now-timedelta(minutes=5)).isoformat(),
        'elapsed_seconds':300,'intake_started_at':(now-timedelta(minutes=20)).isoformat(),
        'targets':targets(),'evidence_artifacts':artifacts,'offsite_accepted':False,
        'execution_ready':False,'environment_rebuilt_offline':True,'recovery_imports_verified':True,'platform':platform}
    write_json(dest/'evidence/completed.json',receipt)
    return dest, receipt, now


@pytest.mark.parametrize('damage',['missing-artifacts','changed-artifact','human-extra','human-future'])
def test_home_collection_rejects_incomplete_or_changed_technical_and_human_evidence(tmp_path, damage):
    from tools.v2.offline_home import collect, CHECKLIST
    dest, receipt, now = technical_receipt(tmp_path)
    human = {name:True for name in CHECKLIST}
    human.update(confirmed_at=(now-timedelta(minutes=1)).isoformat(),operator_alias='home-user')
    if damage == 'missing-artifacts':
        receipt['evidence_artifacts'] = {}
        (dest/'evidence/completed.json').write_text(json.dumps(receipt))
    elif damage == 'changed-artifact':
        (dest/'evidence/historical-market.html').write_text('changed')
    elif damage == 'human-extra':
        human['api_key'] = 'PRIVATE'
    else:
        human['confirmed_at'] = (now+timedelta(days=1)).isoformat()
    write_json(tmp_path/'human.json',human)
    with pytest.raises(ValueError):
        collect(dest,tmp_path/'rejected','a'*64,human_confirmation=tmp_path/'human.json')
    assert not (tmp_path/'rejected').exists()


def test_home_rto_includes_intake_and_human_confirmation_without_certifying_offsite(tmp_path):
    from tools.v2.offline_home import collect, CHECKLIST
    dest, _, now = technical_receipt(tmp_path)
    write_json(tmp_path/'human.json',{**{name:True for name in CHECKLIST},
        'confirmed_at':(now-timedelta(minutes=1)).isoformat(),'operator_alias':'home-user'})
    result = collect(dest,tmp_path/'collected','a'*64,human_confirmation=tmp_path/'human.json')
    assert result['end_to_end_seconds'] == 1140
    assert result['technical_elapsed_seconds'] == 300
    assert result['rto_assessment'] == 'within_target_measured_not_offsite_certification'
    assert result['offsite_accepted'] is False
    assert result['business_summary']['archive_rows'] == 1
    assert result['business_summary']['historical_market_date'] == '2026-09-04'


@pytest.mark.parametrize('damage',['empty-business','unsafe-machine'])
def test_home_collection_requires_business_success_and_typed_machine_fields(tmp_path,damage):
    from tools.v2.offline_home import collect
    dest, receipt, _ = technical_receipt(tmp_path)
    if damage == 'empty-business':
        path = dest/'evidence/business-verification.json'; path.write_text('{}')
        receipt['evidence_artifacts']['evidence/business-verification.json'] = file_hash(path)
    else:
        receipt['platform']['machine_fingerprint'] = 'TOKEN=must-never-collect'
    (dest/'evidence/completed.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        collect(dest,tmp_path/'rejected','a'*64)


def test_failed_home_attempt_retains_outer_receipt_without_completion(tmp_path):
    from tools.v2 import offline_home
    package = tmp_path/'package'; package.mkdir()
    with pytest.raises((FileNotFoundError,ValueError)):
        offline_home.run_home(package,'a'*64,tmp_path/'destination',tmp_path/'attempt',
                              intake_started_at=datetime.now(timezone.utc).isoformat())
    failed = read_json(tmp_path/'attempt/home-technical.json')
    assert failed['status']=='failed' and failed['offsite_accepted'] is False
    assert not (tmp_path/'destination/evidence/completed.json').exists()


def test_incomplete_recovery_collects_only_sanitized_stage_failure(tmp_path):
    from tools.v2.offline_home import collect
    dest = tmp_path/'failed'; (dest/'evidence').mkdir(parents=True)
    (dest/'evidence/capacity-recheck.log').write_text('API_KEY=private-secret\npartial verification')
    result = collect(dest,tmp_path/'collected','a'*64)
    assert result['status']=='technical_incomplete'
    assert result['offsite_accepted'] is False
    log = (tmp_path/'collected/capacity-recheck.log').read_text()
    assert 'private-secret' not in log and 'partial verification' in log


def test_small_candidate_build_preserves_legacy_and_emits_pinned_private_transfer(bound_lab,tmp_path):
    """Fake bootstrap/wheels test packaging only, never an executable runtime."""
    import subprocess
    from test_offline_acceptance import fake_package
    from tools.v2 import build_offline_candidate as builder, offline_package
    root, _ = bound_lab
    actual = Path(__file__).resolve().parents[1]
    old, _ = fake_package(tmp_path)
    legacy = tmp_path/'legacy-lab'; legacy_plan = prepared(legacy)
    shutil.copyfile(root/'inputs/databases/raw.duckdb',legacy/'inputs/databases/market.duckdb')
    recovery_bundle.capture(legacy,legacy_plan,'bundles/initial')
    recovery_bundle.restore(legacy,'bundles/initial','restores/first')
    capacity_acceptance.archive_table(legacy,'inputs/databases/market.duckdb','multi_source_kline',
                                     'restores/first','capacity/prices')
    for member_name in ('rehearsal/rehearsal.json','rehearsal/bundles/initial/completed.json',
                        'rehearsal/capacity/prices/completed.json','package.json'):
        (old/member_name).unlink()
    shutil.copytree(legacy/'bundles',old/'rehearsal/bundles',dirs_exist_ok=True)
    shutil.copytree(legacy/'capacity',old/'rehearsal/capacity',dirs_exist_ok=True)
    shutil.copyfile(legacy/'rehearsal.json',old/'rehearsal/rehearsal.json')
    old_pin = offline_package.seal_package(old,base_sha=file_hash(old/'environment/base.zip'),
        jsonpath_name='jsonpath-0.82.2-py3-none-any.whl',
        jsonpath_sha=file_hash(old/'environment/wheels/jsonpath-0.82.2-py3-none-any.whl'),catalog='capacity/prices')
    old_before = recovery_bundle.inventory(old)
    source = tmp_path/'source-export'; source.mkdir()
    for subfolder, pattern in (('trade_system','*.py'),('tools','*.py'),('migrations','*.sql')):
        for path in (actual/subfolder).rglob(pattern):
            relative = path.relative_to(actual)
            dest = source/relative; dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,dest)
    for name in ('requirements-dev.lock','requirements-data.lock','requirements-build.lock'):
        shutil.copyfile(old/'code'/name,source/name)
    files = {p.relative_to(source).as_posix():file_hash(p) for p in source.rglob('*') if p.is_file()}
    subprocess.run(['git','init','-q',str(source)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(source),'add','.'],check=True,capture_output=True)
    subprocess.run(['git','-C',str(source),'-c','user.name=SYNTHETIC','-c','user.email=synthetic@example.invalid',
                    'commit','-qm','Synthetic source fixture'],check=True,capture_output=True)
    write_json(tmp_path/'source-manifest.json',{'schema':'offline_source_export_v1','files':files})
    pages = tmp_path/'pages'
    shutil.copytree(root/'inputs/raw_facts/pages',pages)
    fixture = read_json(root/'inputs/raw_facts/fixture.json')
    fresh_workspace = Path(fixture['workspace'])
    source_workspace_before = {p.relative_to(fresh_workspace).as_posix():file_hash(p)
                              for p in fresh_workspace.rglob('*') if p.is_file()}
    fixture.update(pages=str(pages))
    write_json(tmp_path/'fixture-receipt.json',fixture)
    write_json(tmp_path/'targets.json',targets())
    plan = {'schema':'offline_candidate_build_plan_v1','source_root':str(source),
        'source_manifest':{'path':str(tmp_path/'source-manifest.json'),'sha256':file_hash(tmp_path/'source-manifest.json')},
        'previous_package':{'path':str(old),'manifest_sha256':old_pin},
        'raw_database_member':'rehearsal/bundles/initial/databases/market.duckdb',
        'migration_receipt':{'path':str(root/'inputs/raw_facts/migration.json'),'sha256':file_hash(root/'inputs/raw_facts/migration.json')},
        'migrated_database':{'path':str(root/'inputs/databases/migrated.duckdb'),'sha256':file_hash(root/'inputs/databases/migrated.duckdb')},
        'observation':{'workspace':str(fresh_workspace),'pages':str(pages),
            'receipt':{'path':str(tmp_path/'fixture-receipt.json'),'sha256':file_hash(tmp_path/'fixture-receipt.json')}},
        'targets':{'path':str(tmp_path/'targets.json'),'sha256':file_hash(tmp_path/'targets.json')},
        'projection':{'day':'2026-09-04','as_of':'2026-10-29T18:00:00+08:00'},'output':str(tmp_path/'candidate')}
    write_json(tmp_path/'build-plan.json',plan)
    result = builder.build(tmp_path/'build-plan.json',file_hash(tmp_path/'build-plan.json'))
    kit = tmp_path/'candidate/portable-package'
    assert offline_package.verify_package(kit,result['manifest_sha256'])['schema'] == 'stockdata_observation_offline_package_v3'
    assert (tmp_path/'candidate/portable-candidate.zip').is_file()
    assert (kit/'RUN-HOME.ps1').is_file() and (kit/'COLLECT-ACCEPTANCE.ps1').is_file()
    assert read_json(tmp_path/'candidate/business-evidence/historical-market.json')['market']['stocks']['000001']['close'] == 10
    assert recovery_bundle.inventory(old) == old_before
    assert source_workspace_before == {p.relative_to(fresh_workspace).as_posix():file_hash(p)
                                     for p in fresh_workspace.rglob('*') if p.is_file()}
    # Even re-sealing the outer membership cannot admit a missing new tool.
    (kit/'code/tools/v2/offline_home.py').unlink()
    manifest = read_json(kit/'package.json'); manifest['files'] = offline_package.inventory(kit)
    (kit/'package.json').write_text(json.dumps(manifest))
    with pytest.raises((ValueError,FileNotFoundError)):
        offline_package.verify_package(kit,file_hash(kit/'package.json'))
