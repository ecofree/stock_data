"""Regression of independently found R1-R4 defects, using isolated fixtures."""
from copy import deepcopy
import json
import shutil
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
from test_isolated_capabilities import prepared
from test_offline_acceptance import fake_package
from trade_system.v2 import acceptance_scope as scope, research_product_view as view
from trade_system.v2 import research_product, operator_workflow, daily_workspace
from trade_system.v2.domain import identity
from trade_system.v2.isolated_workspace import write_json, read_json
from tools.v2 import offline_package as package, prepare_development_runtime as builder
from tools.v2.offline_contract import ENTRYPOINTS, LOCKS, code_closure, wheel_identity
from tools.v2.offline_process import run_stage


@pytest.mark.parametrize('revised',[False,True])
def test_observation_migration_redacts_historical_account_ids_without_changing_records(tmp_path,revised):
    root=tmp_path/'lab';prepared(root);ws=root/'inputs/workspace'
    write_json(ws/'workspace-config.json',{'acceptance_scope':scope.SCOPE})
    note=read_json(next((ws/'notes').glob('*.json')))
    plan={'note_id':note['note_id'],'instrument':note['instrument'],'operator':note['operator'],
          'received_at':'2026-10-20T01:00:00Z','valid_until':'2026-10-29T00:00:00Z',
          'condition':'manual','threshold':'','account_id':'SYNTHETIC-PRIVATE-ACCOUNT',
          'account_snapshot_id':'SYNTHETIC-PRIVATE-SNAPSHOT','execution_ready':False,
          'supersedes':None,'request_id':'3'*32,'command_id':'4'*64,
          'scope':'observation_plan_not_order','invalidation':'synthetic condition'}
    plan['plan_id']=identity(plan);write_json(ws/'notes/plans'/(plan['plan_id']+'.json'),plan)
    if revised:
        newer={k:v for k,v in plan.items() if k!='plan_id'}
        newer.update(supersedes=plan['plan_id'],request_id='5'*32,received_at='2026-10-21T01:00:00Z')
        newer['plan_id']=identity(newer);write_json(ws/'notes/plans'/(newer['plan_id']+'.json'),newer)
    original={p.name:p.read_bytes() for p in (ws/'notes/plans').glob('*.json')}
    data=research_product.journal_projection(ws,daily_workspace.empty_projection());before=deepcopy(data)
    exported=view.export_projection(data);page=view.render(data)
    assert 'SYNTHETIC-PRIVATE' not in page and 'SYNTHETIC-PRIVATE' not in json.dumps(exported)
    assert data==before
    assert {p.name:p.read_bytes() for p in (ws/'notes/plans').glob('*.json')}==original
    for row in exported['plans']['rows']:
        assert 'account_scope_conflict' in row['evaluation']['account_blockers']
        assert row['evaluation']['execution_ready'] is False
    assert exported['plans']['account']['status']=='not_applicable_current_scope'
    assert operator_workflow.read_observation_plan(ws,plan['plan_id'])['account_id']=='SYNTHETIC-PRIVATE-ACCOUNT'


def repin_incomplete(root):
    value=json.loads((root/'package.json').read_text())
    value['files']=package.inventory(root)
    (root/'package.json').write_text(json.dumps(value))
    return package.digest(root/'package.json')


@pytest.mark.parametrize('name',[*('code/'+n for n in (*ENTRYPOINTS,*LOCKS)),
                                'RUN-RESTORE.ps1','README-离机验收.txt','bootstrap/python312.dll',
                                'bootstrap/Lib/encodings/__init__.py'])
def test_seal_and_preflight_reject_omitted_required_material_even_with_fresh_pin(tmp_path,name):
    root,_=fake_package(tmp_path);(root/name).unlink()
    pin=repin_incomplete(root)
    with pytest.raises(ValueError):package.verify_package(root,pin)
    (root/'package.json').unlink()
    with pytest.raises(ValueError):
        package.seal_package(root,base_sha=package.digest(root/'environment/base.zip'),
            jsonpath_name='jsonpath-0.82.2-py3-none-any.whl',
            jsonpath_sha=package.digest(root/'environment/wheels/jsonpath-0.82.2-py3-none-any.whl'),catalog='capacity/prices')
    assert not (root/'package.json').exists()


def test_local_transitive_dependency_omission_rejected_without_importing_code(tmp_path):
    code=tmp_path/'code';code.mkdir()
    for n in (*ENTRYPOINTS,'trade_system/__init__.py','trade_system/v2/__init__.py'):
        p=code/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# synthetic\n')
    (code/'tools/v2/isolated_capabilities.py').write_text('from trade_system.v2 import chain\n')
    (code/'trade_system/v2/chain.py').write_text('from .domain import identity\n')
    with pytest.raises(ValueError,match='domain'):code_closure(code)
    (code/'trade_system/v2/domain.py').write_text('raise RuntimeError("must not execute preflight code")\n')
    assert 'trade_system/v2/domain.py' in code_closure(code)


@pytest.mark.parametrize('nested',[True,False])
def test_wheel_identity_allows_vendored_metadata_but_rejects_multiple_root_distributions(tmp_path,nested):
    path=tmp_path/'sample-1.0-py3-none-any.whl'
    with zipfile.ZipFile(path,'w') as archive:
        for prefix,name in [('sample-1.0.dist-info/','sample'),
                            (('sample/_vendor/' if nested else '')+'other-2.0.dist-info/','other')]:
            archive.writestr(prefix+'METADATA','Name: '+name+'\nVersion: '+('1.0' if name=='sample' else '2.0')+'\n')
            archive.writestr(prefix+'WHEEL','Wheel-Version: 1.0\nTag: py3-none-any\n')
    if nested:
        assert wheel_identity(path)==('sample','1.0',True)
    else:
        with pytest.raises(ValueError,match='ambiguous'):wheel_identity(path)


@pytest.mark.parametrize('damage',['missing','wrong-hash','wrong-version','wrong-platform','wrong-python','metadata'])
def test_wheel_closure_rejects_unusable_material_before_creating_restore_target(tmp_path,damage):
    root,_=fake_package(tmp_path);old=root/'environment/wheels/duckdb-1.0-py3-none-any.whl'
    digest=package.digest(old)
    if damage=='missing':old.unlink()
    elif damage=='wrong-hash':old.write_bytes(b'broken')
    else:
        version='2.0' if damage=='wrong-version' else '1.0'
        tag='cp312-cp312-manylinux_2_17_x86_64' if damage=='wrong-platform' else 'cp313-cp313-win_amd64' if damage=='wrong-python' else 'py3-none-any'
        old.unlink();new=old.with_name('duckdb-'+version+'-'+tag+'.whl')
        with zipfile.ZipFile(new,'w') as archive:
            prefix='duckdb-'+version+'.dist-info/'
            archive.writestr(prefix+'METADATA','Name: '+('different' if damage=='metadata' else 'duckdb')+'\nVersion: '+version+'\n')
            archive.writestr(prefix+'WHEEL','Wheel-Version: 1.0\nTag: '+tag+'\n')
        # Give platform/version counterexamples an admitted byte hash so those
        # checks cannot pass merely because membership detects an altered file.
        for n in LOCKS:
            p=root/'code'/n;p.write_text(p.read_text().replace(digest,package.digest(new)))
    pin=repin_incomplete(root);target=tmp_path/'never-created'
    with pytest.raises((ValueError,zipfile.BadZipFile)):package.restore_package(root,pin,target)
    assert not target.exists()


@pytest.mark.parametrize('phase',['environment-rebuild','recovery-imports','business-restore','capacity-recheck'])
@pytest.mark.parametrize('failure',['timeout','nonzero'])
def test_each_restore_failure_keeps_diagnostics_and_never_completes(tmp_path,monkeypatch,phase,failure):
    root,pin=fake_package(tmp_path);target=tmp_path/'target'
    phases=iter(['environment-rebuild','recovery-imports','business-restore','capacity-recheck'])
    def run(command,**kwargs):
        current=next(phases)
        if current==phase:
            if failure=='timeout':raise subprocess.TimeoutExpired(command,kwargs['timeout'],output=b'partial stdout',stderr=b'API_KEY=synthetic-secret')
            return SimpleNamespace(returncode=1,stdout='partial stdout',stderr='API_KEY=synthetic-secret')
        return SimpleNamespace(returncode=0,stdout='stage passed',stderr='')
    monkeypatch.setattr(package.subprocess,'run',run)
    with pytest.raises((RuntimeError,subprocess.TimeoutExpired)):package.restore_package(root,pin,target)
    log=(target/'evidence'/(phase+'.log')).read_text()
    assert 'partial stdout' in log and 'synthetic-secret' not in log and '[REDACTED]' in log
    status=json.loads((target/'evidence'/(phase+'.json')).read_text())
    assert status['status']==('timeout' if failure=='timeout' else 'failed')
    assert status['started_at'] and status['finished_at'] and status['timeout_seconds']==900
    assert not (target/'evidence/completed.json').exists()


def test_environment_builder_retains_its_inner_timeout(tmp_path,monkeypatch):
    root,_=fake_package(tmp_path);base=tmp_path/'builder';base.mkdir()
    for n in LOCKS:shutil.copyfile(root/'code'/n,base/n)
    def timed_out(command,**kwargs):raise subprocess.TimeoutExpired(command,300,output='prefix diagnostic',stderr='timeout')
    monkeypatch.setattr(builder,'ROOT',base);monkeypatch.setattr(builder.subprocess,'run',timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        builder.prepare(root/'environment/base.zip',package.digest(root/'environment/base.zip'),
            root/'environment/wheels',root/'environment/wheels/jsonpath-0.82.2-py3-none-any.whl',
            package.digest(root/'environment/wheels/jsonpath-0.82.2-py3-none-any.whl'),tmp_path/'evidence')
    assert 'prefix diagnostic' in (tmp_path/'evidence/prefix-before.log').read_text()
    assert not (tmp_path/'evidence/verification.json').exists()


def test_stage_start_failure_and_redaction_keep_existing_logs(tmp_path):
    def failed(*args,**kwargs):raise OSError('Authorization: Bearer synthetic-token')
    with pytest.raises(OSError):run_stage('probe',[],directory=tmp_path,env={},cwd=tmp_path,timeout=1,runner=failed)
    before=(tmp_path/'probe.log').read_bytes()
    assert b'synthetic-token' not in before
    with pytest.raises(FileExistsError):run_stage('probe',[],directory=tmp_path,env={},cwd=tmp_path,timeout=1,runner=failed)
    assert (tmp_path/'probe.log').read_bytes()==before


def test_offline_page_does_not_offer_unavailable_online_launcher():
    data=daily_workspace.empty_projection()
    data.update(acceptance_scope=scope.SCOPE,historical_rehearsal=True,
                plans={'account':scope.account_not_applicable(),'rows':[]})
    html=view.render(data)
    assert 'Start Research.cmd' not in html
    assert '此包用于历史查看和恢复验收' in html and '随包说明' in html
    assert '没有恢复运营' in html
