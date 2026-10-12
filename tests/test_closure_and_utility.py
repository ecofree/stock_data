import os
import sys
import duckdb
import pandas as pd
import pytest

from trade_system import db_utils
from trade_system.v2.utility_verification import evaluate


@pytest.fixture(autouse=True)
def isolate_temporary_git_repositories(request, monkeypatch):
    # These cases create their own Git fixtures, not the source build context.
    if request.node.name.startswith(('test_inventory_', 'test_writer_gate_', 'test_snapshot_writer_')):
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            monkeypatch.delenv(name, raising=False)


def test_installed_legacy_disk_writes_are_denied_but_reads_and_memory_work(tmp_path,monkeypatch):
    target=tmp_path/'not-created.duckdb'
    monkeypatch.setattr(db_utils,'primary_checkout',lambda:None)
    with pytest.raises(ValueError,match='installed runtime'):db_utils.legacy_connect(target)
    assert not target.exists()
    with duckdb.connect(str(target)) as con:con.execute('CREATE TABLE retained(i INTEGER)')
    with db_utils.legacy_connect(target,read_only=True) as con:assert con.execute('SHOW TABLES').fetchall()==[('retained',)]
    with db_utils.legacy_connect(':memory:') as con:con.execute('CREATE TABLE temporary(i INTEGER)')


def test_primary_database_hardlink_alias_cannot_bypass_path_guard(tmp_path,monkeypatch):
    primary=tmp_path/'live';primary.mkdir();source=primary/'kpl_data.duckdb'
    with duckdb.connect(str(source)) as con:con.execute('CREATE TABLE retained(i INTEGER)')
    alias=tmp_path/'alias.duckdb';os.link(source,alias)
    monkeypatch.setattr(db_utils,'primary_checkout',lambda:primary)
    with pytest.raises(ValueError,match='hardlink'):db_utils.legacy_connect(alias)


def frame(days=30,stocks=8):
    return pd.DataFrame([{'datetime':d.strftime('%Y-%m-%d'),'instrument':f'{i:06d}',
        'prediction':float(i),'old_prediction':float(-i),'label_next_ret':float(-i),'ret_20d':float(-i)}
        for d in pd.bdate_range('2025-01-01',periods=days) for i in range(stocks)])


def test_utility_can_conclude_negative_without_promoting_a_model():
    r=evaluate(frame())
    assert r['comparisons']['momentum']['negative_interval']
    assert r['means_pct']['model']<r['means_pct']['momentum']
    assert not r['superiority_proven'] and not r['promotion_allowed']


def test_small_cross_section_cannot_claim_top5_advantage():
    r=evaluate(frame(stocks=5))
    assert r['eligible_days']==0
    assert r['comparisons']['momentum']['status']=='insufficient_eligible_days'


def test_utility_requires_unique_exact_paired_values():
    f=frame()
    with pytest.raises(ValueError,match='duplicate'):evaluate(pd.concat([f,f.iloc[:1]]))
    f.loc[0,'ret_20d']=float('nan')
    with pytest.raises(ValueError,match='nonfinite'):evaluate(f)


def test_inventory_includes_tool_alias_and_dynamic_sql(tmp_path):
    import subprocess
    from tools.v2.closure_readiness import inventory
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/'tools').mkdir()
    (tmp_path/'tools/legacy.py').write_text('import duckdb as db\nc=db.connect(path)\nc.execute(query)\n')
    result=inventory(tmp_path)
    assert {s['kind'] for s in result['sites']}=={'potential_disk_writer','dynamic_sql_requires_connection_boundary'}
    assert not result['all_authority_closed']
    from tools.v2.closure_readiness import writer_gate
    with pytest.raises(ValueError,match='unclassified'):writer_gate(tmp_path)


def _snapshot_writer_fixture(tmp_path, change=None, relative='trade_system/pipeline_runtime.py'):
    import ast
    from pathlib import Path
    import subprocess
    root=Path(__file__).resolve().parents[1]
    source=(root/'trade_system/pipeline_runtime.py').read_text(encoding='utf-8-sig')
    function=next(node for node in ast.parse(source).body
        if isinstance(node,ast.FunctionDef) and node.name=='freeze_operational_inputs')
    source=ast.get_source_segment(source,function)+'\n'
    if change:
        old,new=change
        assert source.count(old)==1
        source=source.replace(old,new)
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    target=tmp_path/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(source,encoding='utf-8')
    return target


def test_writer_gate_classifies_only_reviewed_run_local_snapshot(tmp_path):
    from tools.v2.closure_readiness import inventory,writer_gate
    from trade_system.v2.domain import file_hash
    target=_snapshot_writer_fixture(tmp_path)
    writers=[site for site in inventory(tmp_path)['sites'] if site['kind'].endswith('writer')]
    assert len(writers)==1
    assert writers[0]['kind']=='run_local_operational_snapshot_writer'
    assert writers[0]['function']=='freeze_operational_inputs'
    assert writers[0]['sha256']==file_hash(target)
    assert target.read_text(encoding='utf-8').splitlines()[writers[0]['line']-1].strip()==\
        'output = duckdb.connect(str(partial))'
    result=writer_gate(tmp_path)
    assert result['direct_writer_gate_passed']
    assert result['non_test_sites']=={}
    assert result['classified_writer_sites']==writers


@pytest.mark.parametrize('change',[
    ('def freeze_operational_inputs(', 'def other_writer('),
    ('folder = Path(run_dir).resolve(strict=True)', 'folder = Path(db_path).resolve(strict=True)'),
    ('folder = Path(run_dir).resolve(strict=True)', 'folder = Path(run_dir).resolve(strict=False)'),
    ("partial = folder / ('operational-inputs.' + uuid.uuid4().hex + '.partial.duckdb')", 'partial = Path(db_path)'),
    ("uuid.uuid4().hex + '.partial.duckdb'", "'fixed.partial.duckdb'"),
    ('output = duckdb.connect(str(partial))', 'output = duckdb.connect(str(db_path))'),
    ('output = duckdb.connect(str(partial))', 'partial = Path(db_path)\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'folder = Path(db_path)\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'run_dir = db_path\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'if (partial := Path(db_path)):\n        pass\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'try:\n        pass\n    except Exception as partial:\n        pass\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'import pathlib as partial\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'def partial():\n        pass\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'output = duckdb.connect(str(partial))\n    output = duckdb.connect(str(partial))'),
    ('output = duckdb.connect(str(partial))', 'output = duckdb.connect(str(partial))\n    extra = duckdb.connect(str(db_path))'),
    ('output = duckdb.connect(str(partial))', 'def nested():\n        output = duckdb.connect(str(partial))'),
])
def test_writer_gate_rejects_snapshot_target_scope_or_extra_writer_changes(tmp_path,change):
    from tools.v2.closure_readiness import writer_gate
    _snapshot_writer_fixture(tmp_path,change)
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


def test_snapshot_writer_classification_does_not_follow_function_to_another_file(tmp_path):
    from tools.v2.closure_readiness import writer_gate
    _snapshot_writer_fixture(tmp_path,relative='trade_system/other_runtime.py')
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


def test_delivery_timeout_preserves_real_child_output_and_failure(tmp_path):
    from tools.v2.verify_delivery import _run_check, digest

    command=[sys.executable,'-u','-c',
        "import os,time; os.write(1,b'current_test_name\\xff\\n'); "
        "os.write(2,b'waiting_thread_stack\\n'); time.sleep(30)"]
    result=_run_check(tmp_path,'tests',command,timeout=1)
    log=tmp_path/'tests.log'
    assert result=={'status':'timeout','timeout_seconds':1,'log_sha256':digest(log)}
    assert 'returncode' not in result
    retained=log.read_text(encoding='utf-8')
    assert 'current_test_name\ufffd' in retained
    assert 'waiting_thread_stack' in retained
