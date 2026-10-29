"""Disposable historical migrations preserve facts and fail as one operation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import duckdb
import pytest


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_db(tmp_path, *, duplicate=None):
    path = tmp_path/'original.duckdb'
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE TABLE daily_summary(date DATE,limit_up_count INTEGER,limit_down_count INTEGER,'
                    'rise_count INTEGER,fall_count INTEGER,consecutive_count INTEGER,fetched_at TIMESTAMP,source_kind VARCHAR)')
        con.execute("INSERT INTO daily_summary VALUES ('2026-09-04',0,0,1,1,0,'2026-09-04 16:00:00','real')")
        con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,'
                    'open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,amount DOUBLE,change_pct DOUBLE,'
                    'provider VARCHAR,volume_unit VARCHAR,amount_unit VARCHAR,adjustment VARCHAR,'
                    'fetched_at TIMESTAMP,is_stale BOOLEAN,raw_json VARCHAR)')
        con.execute("INSERT INTO multi_source_kline VALUES ('2026-09-04','stock','000001',10,10,10,10,1,10,0,"
                    "'tushare','shares','yuan',NULL,'2026-09-04 16:00:00',false,'{}')")
        if duplicate is not None:
            if duplicate != 'NULL':
                con.execute('UPDATE multi_source_kline SET adjustment=?', [duplicate])
            con.execute('INSERT INTO multi_source_kline SELECT * FROM multi_source_kline')
        else:
            con.execute('CREATE UNIQUE INDEX uq_multi_source_kline ON multi_source_kline'
                        '(source_date,asset_type,asset_code,provider)')
        con.execute('CREATE VIEW retained_view AS SELECT close FROM multi_source_kline')
        con.execute('CREATE TABLE protected_facts(payload VARCHAR)')
        con.execute("INSERT INTO protected_facts VALUES ('same'),('same'),(NULL)")
    return path


def rehearsal(source, output, **kwargs):
    from trade_system.v2.market_migration_rehearsal import rehearse
    return rehearse(source, output, source_sha256=digest(source), **kwargs)


def test_connection_builder_leaves_connection_and_transaction_owned_by_caller():
    from trade_system.normalize import build_normalized_views_on_connection
    with duckdb.connect(':memory:') as con:
        con.execute('BEGIN')
        views = build_normalized_views_on_connection(con)
        assert 'v_limit_pool_rich' in views
        con.execute('ROLLBACK')
        assert con.execute("SELECT count(*) FROM duckdb_views() WHERE NOT internal").fetchone()[0] == 0
        assert con.execute('SELECT 42').fetchone() == (42,)


def test_positive_preserves_all_columns_multiplicity_unknowns_and_receipt(tmp_path):
    source = source_db(tmp_path)
    before = digest(source)
    receipt = rehearsal(source, tmp_path/'candidate')
    assert receipt['status'] == 'passed'
    assert receipt['source']['sha256'] == before == digest(source)
    assert receipt['candidate']['sha256'] == digest(tmp_path/'candidate'/'migrated-market.duckdb')
    assert receipt['migrations']['applied_versions'] == [14, 15, 16, 17]
    assert receipt['migrations']['unverified_legacy_versions'] == []
    assert receipt['migrations']['missing_prior_versions'] == list(range(1, 14))
    assert all(t['source_minus_candidate'] == t['candidate_minus_source'] == 0
               for t in receipt['preservation']['tables'])
    assert receipt['qualification']['raw_price_identity']['unknown_adjustment_rows'] == 1
    assert receipt['qualification']['market_counts']['daily_summary']['qualified_rows'] == 0
    assert receipt['qualification']['market_counts']['daily_summary']['excluded_rows'] == 1
    assert receipt['qualification']['canonical_prices']['excluded_reasons'] == {'unknown_adjustment': 1}
    assert not receipt['execution_ready'] and not receipt['production_cutover']
    assert json.loads((tmp_path/'candidate'/'migration-receipt.json').read_text(encoding='utf-8')) == receipt


@pytest.mark.parametrize('duplicate', ['none', 'NULL'])
def test_duplicate_full_product_keys_fail_without_dedupe(tmp_path, duplicate):
    source = source_db(tmp_path, duplicate=duplicate)
    output = tmp_path/'failed'
    with pytest.raises(ValueError, match='duplicate product'):
        rehearsal(source, output)
    receipt = json.loads((output/'migration-receipt.json').read_text())
    assert receipt['status'] == 'failed' and receipt['failure']['stage'] == 'preflight'
    with duckdb.connect(str(output/'migrated-market.duckdb'), read_only=True) as con:
        assert con.execute('SELECT count(*) FROM multi_source_kline').fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name='schema_migration'").fetchone()[0] == 0


def test_view_failure_rolls_back_all_migrations_ledgers_old_views_and_indexes(tmp_path, monkeypatch):
    source = source_db(tmp_path)
    from trade_system.v2 import market_migration_rehearsal as module

    def broken(con):
        con.execute('CREATE OR REPLACE VIEW retained_view AS SELECT 99 AS close')
        con.execute('CREATE VIEW new_partial_view AS SELECT 1')
        raise RuntimeError('injected view failure')

    monkeypatch.setattr(module, 'build_normalized_views_on_connection', broken)
    output = tmp_path/'failed'
    with pytest.raises(RuntimeError, match='injected view failure'):
        rehearsal(source, output)
    result = json.loads((output/'migration-receipt.json').read_text())
    assert result['status'] == 'failed' and result['failure']['rolled_back']
    with duckdb.connect(str(output/'migrated-market.duckdb'), read_only=True) as con:
        assert con.execute('SELECT * FROM retained_view').fetchall() == [(10,)]
        assert con.execute("SELECT index_name FROM duckdb_indexes()").fetchall() == [('uq_multi_source_kline',)]
        assert 'count_provenance_json' not in [r[0] for r in con.execute('DESCRIBE daily_summary').fetchall()]
        names = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
        assert not {'schema_migration', 'schema_migration_checksum', 'new_partial_view'} & names


@pytest.mark.parametrize('unsafe', ['existing', 'same', 'overlap', 'wal', 'wrong_sha'])
def test_unsafe_sources_and_destinations_are_rejected(tmp_path, unsafe):
    source = source_db(tmp_path)
    output = tmp_path/'new'
    sha = digest(source)
    if unsafe == 'existing':
        output.mkdir()
    elif unsafe == 'same':
        output = source
    elif unsafe == 'overlap':
        output = source/'nested'
    elif unsafe == 'wal':
        Path(str(source)+'.wal').write_text('active')
    else:
        sha = '0'*64
    from trade_system.v2.market_migration_rehearsal import rehearse
    with pytest.raises((ValueError, FileExistsError)):
        rehearse(source, output, source_sha256=sha)
    assert digest(source) != '0'*64
    if unsafe not in ('existing', 'same'):
        assert not output.exists()


def test_source_mutation_during_operation_cannot_get_success(tmp_path, monkeypatch):
    source = source_db(tmp_path)
    from trade_system.v2 import market_migration_rehearsal as module
    original = module._sha256
    calls = 0

    def changed(path):
        nonlocal calls
        if Path(path) == source:
            calls += 1
            if calls > 1:
                return 'f'*64
        return original(path)

    monkeypatch.setattr(module, '_sha256', changed)
    with pytest.raises(ValueError, match='source.*changed'):
        rehearsal(source, tmp_path/'failed')
    receipt = json.loads((tmp_path/'failed'/'migration-receipt.json').read_text())
    assert receipt['status'] == 'failed'


def test_preservation_failure_rolls_back_and_never_certifies(tmp_path, monkeypatch):
    source = source_db(tmp_path)
    from trade_system.v2 import market_migration_rehearsal as module
    original = module.build_normalized_views_on_connection

    def loses_duplicate(con):
        result = original(con)
        con.execute("DELETE FROM protected_facts WHERE rowid IN (SELECT min(rowid) FROM protected_facts)")
        return result

    monkeypatch.setattr(module, 'build_normalized_views_on_connection', loses_duplicate)
    with pytest.raises(ValueError, match='preservation'):
        rehearsal(source, tmp_path/'failed')
    with duckdb.connect(str(tmp_path/'failed'/'migrated-market.duckdb'), read_only=True) as con:
        assert con.execute('SELECT count(*) FROM protected_facts').fetchone()[0] == 3


def test_link_source_and_link_parent_rejected(tmp_path):
    source = source_db(tmp_path)
    link = tmp_path/'linked.duckdb'
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip('OS does not permit file symlinks')
    with pytest.raises(ValueError, match='link|reparse'):
        rehearsal(link, tmp_path/'out')


def test_legacy_versions_without_checksum_stay_unverified(tmp_path):
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE TABLE schema_migration(version INTEGER PRIMARY KEY,applied_at TIMESTAMP,description VARCHAR)')
        con.execute("INSERT INTO schema_migration SELECT range,'2026-09-04 16:00:00','legacy' FROM range(1,14)")
    result = rehearsal(source, tmp_path/'out')
    assert result['migrations']['unverified_legacy_versions'] == list(range(1, 14))
    assert result['migrations']['missing_prior_versions'] == []


@pytest.mark.parametrize('defect', ['guess_lineage', 'constraint', 'new_fact', 'ledger'])
def test_schema_and_new_unknowns_are_preservation_contracts(tmp_path, monkeypatch, defect):
    source = source_db(tmp_path)
    from trade_system.v2 import market_migration_rehearsal as module
    original = module.build_normalized_views_on_connection

    def corrupt(con):
        views = original(con)
        if defect == 'guess_lineage':
            con.execute("UPDATE daily_summary SET count_provenance_json='{}'")
        elif defect == 'constraint':
            con.execute('ALTER TABLE protected_facts ADD PRIMARY KEY(payload)')
        elif defect == 'new_fact':
            con.execute("INSERT INTO qlib_shadow_evaluation(model_id) VALUES ('invented')")
        else:
            con.execute("INSERT INTO schema_migration(version,description) VALUES (1,'invented history')")
        return views

    # Give the constraint mutation a valid unique/non-null source so the
    # preservation gate, rather than DuckDB's duplicate check, must reject it.
    if defect == 'constraint':
        with duckdb.connect(str(source)) as con:
            con.execute('DELETE FROM protected_facts')
            con.execute("INSERT INTO protected_facts VALUES ('a'),('b')")
    monkeypatch.setattr(module, 'build_normalized_views_on_connection', corrupt)
    with pytest.raises(ValueError, match='preservation'):
        rehearsal(source, tmp_path/'rejected')


def test_qualification_failure_rolls_back_existing_ledger_and_checksum(tmp_path, monkeypatch):
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE TABLE schema_migration(version INTEGER PRIMARY KEY,description VARCHAR)')
        con.execute("INSERT INTO schema_migration VALUES (1,'old')")
        con.execute('CREATE TABLE schema_migration_checksum(version INTEGER PRIMARY KEY,sha256 VARCHAR NOT NULL)')
        con.execute("INSERT INTO schema_migration_checksum VALUES (1,'unavailable historical bytes')")
    from trade_system.v2 import market_migration_rehearsal as module

    def unavailable(*args):
        raise RuntimeError('qualification unavailable')

    monkeypatch.setattr(module, '_qualification', unavailable)
    with pytest.raises(RuntimeError, match='qualification unavailable'):
        rehearsal(source, tmp_path/'failed')
    with duckdb.connect(str(tmp_path/'failed'/'migrated-market.duckdb'), read_only=True) as con:
        assert con.execute('SELECT * FROM schema_migration').fetchall() == [(1, 'old')]
        assert con.execute('SELECT * FROM schema_migration_checksum').fetchall() == [(1, 'unavailable historical bytes')]


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction contract')
@pytest.mark.parametrize('target_kind', ['source', 'output'])
def test_actual_junction_parent_rejected_before_copy(tmp_path, target_kind):
    target = tmp_path/'target'
    target.mkdir()
    source = source_db(target)
    link = tmp_path/'junction'
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                    'New-Item -ItemType Junction -Path '+quote(link)+' -Target '+quote(target)+' | Out-Null'],
                   check=True, capture_output=True, timeout=20)
    try:
        supplied = link/source.name if target_kind == 'source' else source
        output = link/'new' if target_kind == 'output' else tmp_path/'new'
        from trade_system.v2.market_migration_rehearsal import rehearse
        with pytest.raises(ValueError, match='link|reparse'):
            rehearse(supplied, output, source_sha256=digest(source))
        assert not output.exists()
    finally:
        link.rmdir()


def test_reopen_retains_owned_resource_limits_and_capacity_is_checked(tmp_path, monkeypatch):
    source = source_db(tmp_path)
    from trade_system.v2 import market_migration_rehearsal as module
    connect = module.duckdb.connect
    configs = []

    def recording(*args, **kwargs):
        configs.append(kwargs.get('config', {}))
        return connect(*args, **kwargs)

    monkeypatch.setattr(module.duckdb, 'connect', recording)
    result = rehearsal(source, tmp_path/'out')
    assert result['resources']['required_free_bytes'] < result['resources']['observed_free_bytes']
    assert len(configs) == 2
    for config in configs:
        assert config['threads'] == 1 and config['memory_limit'] == '1GB'
        assert config['max_temp_directory_size']
        assert Path(config['temp_directory']).parent == tmp_path/'out'


def test_insufficient_scratch_capacity_rejected_before_copy(tmp_path, monkeypatch):
    source = source_db(tmp_path)
    from trade_system.v2 import market_migration_rehearsal as module
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: type('Usage', (), {'free': 1})())
    with pytest.raises(ValueError, match='capacity'):
        rehearsal(source, tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_legacy_ledger_label_cannot_replace_required_schema(tmp_path):
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE TABLE schema_migration(version INTEGER PRIMARY KEY,description VARCHAR)')
        con.execute("INSERT INTO schema_migration SELECT range,'unverified label' FROM range(14,18)")
    with pytest.raises(ValueError, match='migration postcondition'):
        rehearsal(source, tmp_path/'failed')


@pytest.mark.parametrize('kind', ['view', 'index'])
def test_unreviewed_new_schema_objects_reject_candidate(tmp_path, monkeypatch, kind):
    source = source_db(tmp_path)
    from trade_system.v2 import market_migration_rehearsal as module
    original = module.build_normalized_views_on_connection

    def adds_unrelated(con):
        views = original(con)
        con.execute('CREATE VIEW unreviewed AS SELECT 1' if kind == 'view'
                    else 'CREATE INDEX unreviewed ON protected_facts(payload)')
        return views

    monkeypatch.setattr(module, 'build_normalized_views_on_connection', adds_unrelated)
    with pytest.raises(ValueError, match='preservation'):
        rehearsal(source, tmp_path/'out')


def test_fingerprints_support_installed_migration_data_location(tmp_path, monkeypatch):
    from trade_system.v2 import market_migration_rehearsal as module
    migration = tmp_path/'share/stock_data/migrations/0017_daily_count_provenance.sql'
    migration.parent.mkdir(parents=True)
    migration.write_text('SELECT 1;', encoding='utf-8')
    monkeypatch.setattr(module, 'discover_migrations', lambda: [(17, migration)])
    assert module._fingerprints()['migrations/'+migration.name] == digest(migration)


@pytest.mark.parametrize('case', ['product_index_on_other_table', 'old_index_on_other_table',
                                   'old_secondary_index_on_other_table'])
def test_reserved_index_names_on_other_tables_fail_without_index_loss(tmp_path, case):
    """Independent-review counterexamples: name/expressions do not prove owner."""
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        if case == 'product_index_on_other_table':
            con.execute('CREATE TABLE unrelated_product(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,'
                        'provider VARCHAR,adjustment VARCHAR)')
            con.execute('CREATE UNIQUE INDEX uq_multi_source_kline_product ON unrelated_product'
                        '(source_date,asset_type,asset_code,provider,adjustment)')
        else:
            name = 'uq_multi_source_kline' if case == 'old_index_on_other_table' else 'ix_multi_source_kline'
            if name == 'uq_multi_source_kline':
                con.execute('DROP INDEX uq_multi_source_kline')
            con.execute(f'CREATE INDEX {name} ON protected_facts(payload)')
        before = con.execute('SELECT schema_name,index_name,table_name,is_unique,expressions,sql '
                             'FROM duckdb_indexes() ORDER BY index_name').fetchall()
    source_hash = digest(source)
    with pytest.raises(ValueError, match='index ownership'):
        rehearsal(source, tmp_path/'rejected')
    assert digest(source) == source_hash
    receipt = json.loads((tmp_path/'rejected'/'migration-receipt.json').read_text(encoding='utf-8'))
    assert receipt['status'] == 'failed' and receipt['failure']['stage'] == 'preflight'
    with duckdb.connect(str(tmp_path/'rejected'/'migrated-market.duckdb'), read_only=True) as con:
        assert con.execute('SELECT schema_name,index_name,table_name,is_unique,expressions,sql '
                           'FROM duckdb_indexes() ORDER BY index_name').fetchall() == before
        assert 'schema_migration' not in {r[0] for r in con.execute('SHOW TABLES').fetchall()}


def test_correct_owned_product_index_and_old_secondary_index_remain_supported(tmp_path):
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE UNIQUE INDEX uq_multi_source_kline_product ON multi_source_kline'
                    '(source_date,asset_type,asset_code,provider,adjustment)')
        con.execute('CREATE INDEX ix_multi_source_kline ON multi_source_kline(source_date,asset_type,asset_code,provider)')
    receipt = rehearsal(source, tmp_path/'candidate')
    assert receipt['status'] == 'passed'
    assert set(receipt['preservation']['allowed_removed_indexes']) == {'uq_multi_source_kline', 'ix_multi_source_kline'}
    removed = {row['name']: row for row in receipt['preservation']['allowed_removed_index_objects']}
    for name, row in removed.items():
        assert row['schema'] == 'main' and row['table'] == 'multi_source_kline'
        assert row['is_unique'] == (name == 'uq_multi_source_kline')
        assert row['expressions'] == ['source_date', 'asset_type', 'asset_code', 'provider']
        assert row['original_sql']
    with duckdb.connect(str(tmp_path/'candidate'/'migrated-market.duckdb'), read_only=True) as con:
        assert con.execute('SELECT schema_name,table_name,index_name FROM duckdb_indexes()').fetchall() == [
            ('main', 'multi_source_kline', 'uq_multi_source_kline_product')]


@pytest.mark.parametrize('name,unique,columns', [
    ('uq_multi_source_kline', True, 'asset_code'),
    ('uq_multi_source_kline', False, 'source_date,asset_type,asset_code,provider'),
    ('ix_multi_source_kline', False, 'source_date,asset_code'),
    ('ix_multi_source_kline', True, 'source_date,asset_type,asset_code,provider'),
])
def test_reserved_legacy_name_on_target_with_unknown_definition_is_preserved(tmp_path, name, unique, columns):
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('DROP INDEX uq_multi_source_kline')
        con.execute(f'CREATE {"UNIQUE " if unique else ""}INDEX {name} ON multi_source_kline({columns})')
        before = con.execute('SELECT index_name,sql FROM duckdb_indexes()').fetchall()
    with pytest.raises(ValueError, match='index definition'):
        rehearsal(source, tmp_path/'rejected')
    with duckdb.connect(str(tmp_path/'rejected'/'migrated-market.duckdb'), read_only=True) as con:
        assert con.execute('SELECT index_name,sql FROM duckdb_indexes()').fetchall() == before


def test_quoted_comma_identifier_cannot_impersonate_four_index_expressions(tmp_path):
    source = source_db(tmp_path)
    with duckdb.connect(str(source)) as con:
        con.execute('DROP INDEX uq_multi_source_kline')
        con.execute('ALTER TABLE multi_source_kline ADD COLUMN "source_date,asset_type" VARCHAR')
        con.execute('CREATE UNIQUE INDEX uq_multi_source_kline ON multi_source_kline'
                    '("source_date,asset_type",asset_code,provider)')
    with pytest.raises(ValueError, match='index definition'):
        rehearsal(source, tmp_path/'rejected')


@pytest.mark.parametrize('wrong_owner', ['table', 'schema'])
def test_postcondition_itself_rejects_product_index_on_wrong_owner(tmp_path, wrong_owner):
    source = source_db(tmp_path)
    from trade_system.migrations import apply_pending_in_transaction
    from trade_system.v2.market_migration_rehearsal import _migration_postconditions
    with duckdb.connect(str(source)) as con:
        con.execute('BEGIN')
        apply_pending_in_transaction(con, versions=(14, 15, 16, 17))
        con.execute('DROP INDEX uq_multi_source_kline_product')
        if wrong_owner == 'schema':
            con.execute('CREATE SCHEMA unrelated_schema')
        owner = 'unrelated_product' if wrong_owner == 'table' else 'unrelated_schema.multi_source_kline'
        con.execute(f'CREATE TABLE {owner}(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,'
                    'provider VARCHAR,adjustment VARCHAR)')
        con.execute(f'CREATE UNIQUE INDEX uq_multi_source_kline_product ON {owner}'
                    '(source_date,asset_type,asset_code,provider,adjustment)')
        with pytest.raises(ValueError, match='migration postcondition'):
            _migration_postconditions(con)
        con.execute('ROLLBACK')
