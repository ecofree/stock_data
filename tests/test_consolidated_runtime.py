import hashlib
import subprocess
import sys
from pathlib import Path
import zipfile

import duckdb
import pytest

from scripts.reclaim_disk_space import inspect_database


def test_shared_writer_refuses_primary_before_creating_file(tmp_path,monkeypatch):
    from trade_system import db_utils
    protected=tmp_path/'live';protected.mkdir()
    monkeypatch.setattr(db_utils,'primary_checkout',lambda:protected)
    target=protected/'must-not-exist.duckdb'
    with pytest.raises(ValueError,match='primary checkout'):
        db_utils.legacy_connect(target)
    assert not target.exists()
    with db_utils.legacy_connect(tmp_path/'isolated.duckdb') as con:
        con.execute('CREATE TABLE allowed(x INTEGER)')


def test_git_free_source_identity_preserves_installed_and_database_boundaries(tmp_path, monkeypatch):
    import json
    from trade_system import db_utils
    original = Path(db_utils.__file__).resolve().parents[1]
    source = tmp_path/'release'; source.mkdir()
    identity_files = ('pyproject.toml', 'scripts/run_integrated_daily.py',
                      'trade_system/db_utils.py', 'trade_system/config.py', 'trade_system/migration_boundary.py')
    files = {}
    for name in identity_files:
        target = source/name; target.parent.mkdir(parents=True, exist_ok=True)
        raw = (original/name).read_bytes(); target.write_bytes(raw)
        files[name] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(db_utils, '__file__', str(source/'trade_system/db_utils.py'))
    target = tmp_path/'fixture.duckdb'
    with pytest.raises(ValueError, match='installed runtime refuses'):
        db_utils.legacy_connect(target)
    assert not target.exists()
    manifest = source/'source-distribution.json'
    manifest.write_text(json.dumps({'schema':'source_distribution_v1',
        'scope':'source_identity_only_not_deployment_authority', 'files':files}), encoding='utf-8')
    assert db_utils.primary_checkout() == source
    with db_utils.legacy_connect(target) as con:
        con.execute('CREATE TABLE fixture(x INTEGER)')
    with pytest.raises(ValueError, match='primary checkout'):
        db_utils.legacy_connect(source/'wrong.duckdb')
    runtime = tmp_path/'runtime'; (runtime/'market').mkdir(parents=True)
    monkeypatch.setenv('STOCKDATA_RUNTIME_ROOT', str(runtime))
    with pytest.raises(ValueError, match='unbound database'):
        db_utils.legacy_connect(target)
    bound = runtime/'market/kpl_data.duckdb'
    with db_utils.legacy_connect(bound) as con:
        con.execute('CREATE TABLE v2_schema(version INTEGER)')
    with pytest.raises(ValueError, match='refuses V2'):
        db_utils.legacy_connect(bound)
    (source/'trade_system/db_utils.py').write_text('# changed source')
    with pytest.raises(ValueError, match='installed runtime refuses'):
        db_utils.legacy_connect(bound)


def test_direct_schema_calls_reject_v2_even_with_runtime_flag(tmp_path,monkeypatch):
    from trade_system.schema import init_schema,_refresh_default_concept_views,_ensure_business_indexes
    monkeypatch.setenv('KPL_RUNTIME_SCHEMA_READY','1')
    db=tmp_path/'account.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE v2_schema(version INTEGER)')
        before=con.execute('SHOW TABLES').fetchall()
        for entry in (init_schema,_refresh_default_concept_views,_ensure_business_indexes):
            with pytest.raises(ValueError,match='refuses V2'):
                entry(con)
        assert con.execute('SHOW TABLES').fetchall()==before


def test_writer_inventory_distinguishes_unknown_from_literal_readonly(tmp_path,monkeypatch):
    from tools.v2 import verify_consolidated as verify
    (tmp_path/'sample.py').write_text('import duckdb as d\nfrom duckdb import connect as c\na=d.connect("a",read_only=True)\nb=c("b",read_only=flag)\nb.execute("CREATE TABLE x(i INTEGER)")\n')
    monkeypatch.setattr(verify,'source_files',lambda:{'sample.py':'fixture-hash'})
    inventory=verify.writer_inventory(tmp_path)
    assert inventory['direct_writer_sites']==1 and inventory['ddl_sites']==1
    assert inventory['all_writer_authority_closed'] is False


def test_legacy_automatic_compaction_refuses_before_opening_database(tmp_path):
    missing = tmp_path/'do-not-create.duckdb'
    script = Path(__file__).resolve().parents[1]/'scripts/reclaim_disk_space.py'
    result = subprocess.run([sys.executable, str(script), '--db', str(missing), '--yes'], capture_output=True)
    assert result.returncode != 0 and b'retired' in result.stderr
    assert not missing.exists()


def test_maintenance_dry_inspection_does_not_modify_database(tmp_path):
    db = tmp_path/'facts.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE keep_me AS SELECT 42 AS value')
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    result = inspect_database(db)
    assert result['source_modified'] is False and result['tables'] == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
    assert not db.with_suffix('.duckdb.wal').exists()


def test_research_distribution_runs_outside_checkout_and_rejects_upgrade_residue(tmp_path):
    from tools.v2.package_research import build
    archive=tmp_path/'research.zip'
    result=build(archive)
    assert result['scope']=='isolated_research_source_distribution_not_operational_core'
    target=tmp_path/'relocated'
    with zipfile.ZipFile(archive) as package:
        assert 'requirements-research-replay.lock' in package.namelist()
        assert b'--require-hashes -r requirements-research-replay.lock' in package.read('README.txt')
        assert not any(p.startswith('tools/') for p in package.namelist())
        assert 'base.py' not in package.namelist()
        assert 'trade_system/data_store.py' not in package.namelist()
        assert 'trade_system/tushare_relay.py' not in package.namelist()
        package.extractall(target)
    command=[sys.executable,'-I',str(target/'run_research.py'),'--help']
    clean=subprocess.run(command,cwd=tmp_path,capture_output=True)
    assert clean.returncode==0,clean.stderr.decode(errors='replace')
    assert b'preflight' in clean.stdout and b'price-study' in clean.stdout
    (target/'legacy_residue.py').write_text('raise RuntimeError("never import")',encoding='utf-8')
    refused=subprocess.run(command,cwd=tmp_path,capture_output=True)
    assert refused.returncode!=0 and b'unexpected' in refused.stderr


def test_base_import_creates_missing_log_directory(tmp_path):
    target = tmp_path / 'new-runtime' / 'logs'
    code = (
        'from trade_system import config; import sys; '
        'config.LOG_DIR = sys.argv[1]; '
        'from trade_system import data_store as base; '
        "base.logger.warning('clean import regression')"
    )
    result = subprocess.run(
        [sys.executable, '-c', code, str(target)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    logs = list(target.glob('collect_*.log'))
    assert len(logs) == 1
    assert 'clean import regression' in logs[0].read_text(encoding='utf-8')
