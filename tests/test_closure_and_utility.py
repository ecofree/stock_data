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


def _isolated_writer_fixture(tmp_path, change=None):
    from pathlib import Path
    import subprocess
    root=Path(__file__).resolve().parents[1]
    files=('trade_system/v2/market_migration_rehearsal.py','trade_system/v2/offline_binding.py',
           'trade_system/v2/isolated_workspace.py','trade_system/db_utils.py')
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    for name in files:
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((root/name).read_bytes())
    if change:
        name,old,new=change;target=tmp_path/name;source=target.read_text('utf-8-sig')
        assert old in source
        target.write_text(source.replace(old,new),encoding='utf-8')


def test_writer_gate_classifies_exact_isolated_artifact_calls_and_keeps_allowed(tmp_path):
    from tools.v2.closure_readiness import writer_gate
    _isolated_writer_fixture(tmp_path)
    result=writer_gate(tmp_path)
    assert result['direct_writer_gate_passed']
    assert result['non_test_sites']=={'trade_system/db_utils.py':1}
    sites=result['classified_writer_sites']
    assert {(s['file'],s['function'],s['kind']) for s in sites}=={
        ('trade_system/v2/market_migration_rehearsal.py','rehearse','isolated_historical_migration_writer'),
        ('trade_system/v2/offline_binding.py','verify_normalized_definitions','isolated_normalization_metadata_writer')}
    assert all(s['contract_id'] and len(s['contract_sha256'])==64 for s in sites)


@pytest.mark.parametrize('change',[
 ('trade_system/v2/market_migration_rehearsal.py',"con = duckdb.connect(str(candidate), config=config)","con = duckdb.connect(str(source), config=config)"),
 ('trade_system/v2/market_migration_rehearsal.py',"source.open('rb')","source.open('wb')"),
 ('trade_system/v2/market_migration_rehearsal.py',"candidate.open('xb')","candidate.open('wb')"),
 ('trade_system/v2/market_migration_rehearsal.py',"if output.exists():","if False:"),
 ('trade_system/v2/market_migration_rehearsal.py',"source, output = _validate_paths(source, output, source_sha256)","source, output = Path(source), Path(output)"),
 ('trade_system/v2/market_migration_rehearsal.py',"with _source_read_guard(source):","with source.open('rb'):"),
 ('trade_system/v2/market_migration_rehearsal.py',"0x80000000, 1, None, 3, 0x80","0x80000000, 7, None, 3, 0x80"),
 ('trade_system/v2/market_migration_rehearsal.py',"if source.samefile(candidate) or _sha256(candidate) != expected:","if False:"),
 ('trade_system/v2/offline_binding.py',"database = scratch/'definitions.duckdb'","database = source"),
 ('trade_system/v2/offline_binding.py',"scratch = safe_path(scratch)","scratch = Path(scratch)"),
 ('trade_system/v2/offline_binding.py',"scratch.mkdir(exist_ok=False)","scratch.mkdir(exist_ok=True)"),
 ('trade_system/v2/offline_binding.py',"AS retained (READ_ONLY)","AS retained"),
 ('trade_system/v2/offline_binding.py',"read_only=True,config=configuration","read_only=False,config=configuration"),
 ('trade_system/v2/offline_binding.py',"database = scratch/'definitions.duckdb'","database = scratch/'definitions.duckdb'\n    database = source"),
 ('trade_system/v2/offline_binding.py',"with duckdb.connect(str(database),config=configuration) as con:","with duckdb.connect(str(database),config=configuration) as con:\n        extra = duckdb.connect(str(source))"),
 ('trade_system/v2/isolated_workspace.py',"return path.resolve()","return path"),
 ('trade_system/db_utils.py',"def primary_checkout():","def renamed_primary_checkout():"),
 ('trade_system/db_utils.py',"root = Path(__file__).resolve().parents[1]","root = Path('C:/wrong-root')"),
 ('trade_system/v2/offline_binding.py',"name = record['path']","global safe_path\n    safe_path = lambda value: value\n    name = record['path']"),
 ('trade_system/v2/offline_binding.py',"name = record['path']","duckdb.connect = lambda *a,**k: None\n    name = record['path']"),
])
def test_writer_gate_rejects_isolated_path_source_guard_or_extra_writer_mutations(tmp_path,change):
    from tools.v2.closure_readiness import writer_gate
    _isolated_writer_fixture(tmp_path,change)
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


@pytest.mark.parametrize('suffix',[
 '\nduckdb = object()\n', '\nduckdb.connect = lambda *a,**k: None\n',
 '\nsafe_path = lambda value: value\n','\nfrom pathlib import Path as safe_path\n',
 '\nmigration = object()\n','\nPath = object()\n','\nstr = lambda value: value\n',
 '\nverify_normalized_definitions = lambda *a,**k: None\n',
 '\ndef verify_normalized_definitions(source,scratch,*,config):\n    return None\n',
 '\ndef extra_writer(source):\n    return duckdb.connect(source)\n',
 '\ndef rebind_guard():\n    global safe_path\n    safe_path = lambda value: value\n',
 '\ndef change_duckdb():\n    duckdb.connect = lambda *a,**k: None\n',
])
def test_writer_gate_rejects_module_alias_shadowing_even_with_original_definition(tmp_path,suffix):
    from tools.v2.closure_readiness import writer_gate
    _isolated_writer_fixture(tmp_path)
    target=tmp_path/'trade_system/v2/offline_binding.py'
    target.write_text(target.read_text('utf-8-sig')+suffix,encoding='utf-8')
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


def test_writer_gate_contract_ast_is_stable_for_empty_python312_metadata():
    import ast
    from tools.v2.closure_readiness import _contract_digest
    node=ast.parse('def fixture(value):\n    return value\n').body[0]
    node._fields=tuple(name for name in node._fields if name!='type_params')
    before=_contract_digest(node)
    node._fields=(*node._fields,'type_params');node.type_params=[]
    assert _contract_digest(node)==before
    node.body[0].value=ast.Constant(7)
    assert _contract_digest(node)!=before


@pytest.mark.parametrize('name,suffix',[
 ('trade_system/v2/market_migration_rehearsal.py','\n_validate_paths = lambda source,output,expected: (source,output)\n'),
 ('trade_system/v2/market_migration_rehearsal.py',"\n_source_read_guard = lambda value: value.open('rb')\n"),
 ('trade_system/db_utils.py','\nprimary_checkout = lambda: None\n'),
 ('trade_system/v2/isolated_workspace.py','\nsafe_path = lambda value: value\n'),
])
def test_writer_gate_guard_helper_rebinding_does_not_leave_original_def_approved(tmp_path,name,suffix):
    from tools.v2.closure_readiness import writer_gate
    _isolated_writer_fixture(tmp_path)
    target=tmp_path/name;target.write_text(target.read_text('utf-8-sig')+suffix,encoding='utf-8')
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


def test_writer_gate_unknown_ast_value_remains_unclassified(tmp_path):
    from tools.v2.closure_readiness import writer_gate
    _isolated_writer_fixture(tmp_path)
    target=tmp_path/'trade_system/v2/offline_binding.py'
    target.write_text(target.read_text('utf-8-sig')+'\nUNKNOWN_REVIEWED_VALUE = 1j\n',encoding='utf-8')
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


def test_writer_gate_isolated_classification_does_not_follow_code_to_other_module(tmp_path):
    from tools.v2.closure_readiness import writer_gate
    _isolated_writer_fixture(tmp_path)
    source=tmp_path/'trade_system/v2/offline_binding.py'
    (source.parent/'other_offline_binding.py').write_bytes(source.read_bytes())
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


@pytest.mark.parametrize('relative,helper,statement',[
 ('trade_system/v2/offline_binding.py','bound_file','path_type = Path\npath_type.mkdir = lambda *a, **k: None'),
 ('trade_system/v2/offline_binding.py','bound_file','db_module = duckdb\ndb_module.connect = lambda *a, **k: None'),
 ('trade_system/v2/offline_binding.py','normalized_members','unused = 7'),
 ('trade_system/v2/isolated_workspace.py','initialize','path_type = Path\npath_type.mkdir = lambda *a, **k: None'),
 ('trade_system/v2/market_migration_rehearsal.py','_q','unused = 7'),
 ('trade_system/db_utils.py','latest_open_session','unused = 7'),
])
def test_writer_gate_rejects_unreviewed_helper_bodies_in_complete_module_contract(tmp_path,relative,helper,statement):
    import ast
    from tools.v2.closure_readiness import writer_gate
    _isolated_writer_fixture(tmp_path)
    target=tmp_path/relative
    tree=ast.parse(target.read_text('utf-8-sig'))
    owner=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==helper)
    owner.body[0:0]=ast.parse(statement).body
    # Only parse the altered fixture; no modified helper or alias is executed.
    target.write_text(ast.unparse(tree),encoding='utf-8')
    with pytest.raises(ValueError,match='new unclassified direct writer'):
        writer_gate(tmp_path)


def test_delivery_timeout_preserves_real_child_output_and_failure(tmp_path):
    from tools.v2.verify_delivery import _run_check, digest

    command=[sys.executable,'-u','-c',
        "import os,time; os.write(1,b'current_test_name\\xff\\n'); "
        "os.write(2,b'waiting_thread_stack\\n'); time.sleep(30)"]
    result=_run_check(tmp_path,'tests',command,timeout=1)
    log=tmp_path/'tests.log'
    import math
    elapsed=result.pop('elapsed_seconds')
    assert math.isfinite(elapsed) and elapsed >= 1
    assert result=={'status':'timeout','timeout_seconds':1,'log_sha256':digest(log)}
    assert 'returncode' not in result
    retained=log.read_text(encoding='utf-8')
    assert 'current_test_name\ufffd' in retained
    assert 'waiting_thread_stack' in retained
