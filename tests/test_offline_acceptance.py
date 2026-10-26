"""Current observation scope and bounded historical capacity counterexamples."""
from copy import deepcopy
import json
import duckdb
import pytest

from test_isolated_capabilities import prepared
from trade_system.v2 import acceptance_scope as scope, accounts, operator_workflow, daily_workspace
from trade_system.v2 import recovery_bundle, capacity_acceptance as capacity, research_product_view, offline_report
from trade_system.v2.isolated_workspace import write_json, read_json, seal
from tools.v2 import offline_package as package


@pytest.fixture
def lab(tmp_path):
    root=tmp_path/'lab';plan=prepared(root)
    write_json(root/'inputs/workspace/workspace-config.json',{'acceptance_scope':scope.SCOPE})
    return root,plan


def captured(lab):
    root,plan=lab
    recovery_bundle.capture(root,plan,'bundles/initial')
    recovery_bundle.restore(root,'bundles/initial','restores/first')
    return root


def test_observation_scope_never_reads_configured_account(lab,monkeypatch):
    root,_=lab;folder=root/'inputs/workspace'
    monkeypatch.setattr(accounts,'risk_snapshot',lambda *a:pytest.fail('account opened'))
    account=operator_workflow.configured_account_risk(folder,'2026-10-23T10:00:00+08:00')
    assert scope.observation_only(account) and not account['execution_ready']
    assert all(r['status']=='not_applicable_current_scope' for r in accounts.configured_performance(folder,'2026-10-23').values())
    assert daily_workspace.action_queue({'plans':{'account':account,'rows':[]}})==[]


@pytest.mark.parametrize('config',[{'acceptance_scope':'unknown'},
    {'acceptance_scope':scope.SCOPE,'account_database':'must-not-open.duckdb'},
    {'acceptance_scope':scope.SCOPE,'account_id':'real-account'}])
def test_unknown_or_account_conflicting_scope_blocks_before_capture(lab,config):
    root,plan=lab
    (root/'inputs/workspace/workspace-config.json').write_text(json.dumps(config))
    with pytest.raises(ValueError):operator_workflow.configured_account_risk(root/'inputs/workspace','2026-10-23T10:00:00+08:00')
    with pytest.raises(ValueError):recovery_bundle.capture(root,plan,'bad-bundle')
    assert not (root/'bad-bundle/completed.json').exists()


def test_historical_scope_keeps_account_missing_gate(tmp_path):
    account=operator_workflow.configured_account_risk(tmp_path,'2026-10-23T10:00:00+08:00')
    assert account['status']=='account_unknown'
    assert daily_workspace.action_queue({'plans':{'account':account}})[0]['kind']=='account_verification'


def test_old_account_bound_plan_not_silently_requalified():
    plan={'received_at':'2026-10-23T00:00:00Z','valid_until':'2026-10-24T00:00:00Z','condition':'manual','account_id':'old'}
    result=operator_workflow.evaluate_observation_plan(plan,'2026-10-23T01:00:00Z',account=scope.account_not_applicable())
    assert 'account_scope_conflict' in result['account_blockers'] and not result['execution_ready']
    plan.pop('account_id')
    assert operator_workflow.evaluate_observation_plan(plan,'2026-10-23T01:00:00Z',account=scope.account_not_applicable())['account_blockers']==[]


def test_actual_current_page_preserves_not_applicable_and_no_privacy_alarm():
    data=daily_workspace.empty_projection()
    data.update(acceptance_scope=scope.SCOPE,plans={'account':scope.account_not_applicable(),'rows':[]},
                action_queue=[{'kind':'observation_plan','priority':2,'instrument':'000001','reason':'pending','allowed_action':'observe'}],
                account_periods={'month':{'status':'not_applicable_current_scope'}})
    projection=research_product_view.export_projection(data)
    assert projection['plans']['account']['status']=='not_applicable_current_scope'
    assert all(r['kind']!='account_privacy' for r in projection['action_queue'])
    page=research_product_view.render(data)
    assert '真实账户对账本阶段不适用' in page and 'market_research_observation_v1' in page
    data['plans']['account']['positions']=[{'quantity':10}]
    with pytest.raises(ValueError,match='conflicting'):research_product_view.render(data)


def test_raw_archive_preserves_unknown_keys_and_duplicate_values(lab):
    root,_=lab;source='inputs/databases/market.duckdb'
    with duckdb.connect(str(root/source)) as con:
        con.execute('INSERT INTO multi_source_kline SELECT * FROM multi_source_kline LIMIT 1')
        con.execute('UPDATE multi_source_kline SET adjustment=NULL')
    captured(lab)
    result=capacity.archive_table(root,source,'multi_source_kline','restores/first','capacity/prices',batch_rows=2)
    assert result['rows']==4 and result['batches']==2
    checked=capacity.verify_table(root,'capacity/prices','restores/first')
    assert checked['complete_multiset_verified'] and not checked['qualified_business_keys_certified']
    assert capacity.archive_table(root,source,'multi_source_kline','restores/first','capacity/prices',batch_rows=2)['receipt_id']==result['receipt_id']


@pytest.mark.parametrize('damage',['omit','repeat','reorder','window','value','extra-file','receipt'])
def test_capacity_coverage_and_values_cannot_be_faked(lab,damage):
    root=captured(lab);folder=root/'capacity/prices'
    capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices',batch_rows=1)
    value=read_json(folder/'completed.json')
    if damage=='omit':value['parts'].pop()
    elif damage=='repeat':value['parts'][1]=deepcopy(value['parts'][0])
    elif damage=='reorder':value['parts'].reverse()
    elif damage=='window':value['ranges'][-1][1]+=1
    elif damage=='value':(folder/'000000.parquet').write_bytes(b'bad')
    elif damage=='extra-file':(folder/'extra.parquet').write_bytes(b'extra')
    else:(folder/'000000.json').write_text('{}')
    value.pop('receipt_id');(folder/'completed.json').write_text(json.dumps(seal(value)))
    with pytest.raises((ValueError,KeyError)):capacity.verify_table(root,'capacity/prices','restores/first')


def test_partial_batch_completion_resumes_without_overwriting(lab,monkeypatch):
    root=captured(lab);original=capacity.write_json
    def broken(path,value):
        if path.name=='000001.json':raise OSError('simulated interruption')
        original(path,value)
    with monkeypatch.context() as m:
        m.setattr(capacity,'write_json',broken)
        with pytest.raises(OSError):capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices',batch_rows=1)
    assert not (root/'capacity/prices/completed.json').exists()
    capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices',batch_rows=1)
    assert capacity.verify_table(root,'capacity/prices','restores/first')['rows']==3


def test_valid_parquet_resealed_with_wrong_values_still_fails(lab):
    root=captured(lab);folder=root/'capacity/prices'
    capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices')
    path=folder/'000000.parquet'
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE changed AS SELECT * REPLACE (close+1 AS close) FROM read_parquet(?)',[str(path)])
        path.unlink()  # Only this test's new synthetic file.
        con.execute('COPY changed TO ? (FORMAT PARQUET)',[str(path)])
    value=read_json(folder/'completed.json');part=value['parts'][0]
    part.update(sha256=package.digest(path),bytes=path.stat().st_size)
    (folder/'000000.json').write_text(json.dumps(seal(part)))
    value.pop('receipt_id');(folder/'completed.json').write_text(json.dumps(seal(value)))
    with pytest.raises(ValueError,match='values'):capacity.verify_table(root,'capacity/prices','restores/first')


def test_verified_empty_table_is_not_confused_with_missing_partition(lab):
    root,_=lab
    with duckdb.connect(str(root/'inputs/databases/market.duckdb')) as con:con.execute('DELETE FROM multi_source_kline')
    captured(lab)
    capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices')
    result=capacity.verify_table(root,'capacity/prices','restores/first')
    assert result['rows']==0 and result['batches']==0 and result['complete_multiset_verified']


@pytest.mark.parametrize('count',[0,100001,True])
def test_capacity_never_raises_memory_budget_to_admit_large_work(lab,count):
    root=captured(lab)
    with pytest.raises(ValueError):capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices',batch_rows=count)


def test_source_change_requires_new_restore_baseline(lab):
    root=captured(lab)
    with duckdb.connect(str(root/'inputs/databases/market.duckdb')) as con:con.execute('DELETE FROM multi_source_kline')
    with pytest.raises(ValueError,match='restored'):capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices')


def fake_package(tmp_path):
    """Structurally complete synthetic materials; never an executable recovery."""
    import zipfile
    from tools.v2.offline_contract import ENTRYPOINTS, LOCKS, CORE_PACKAGES
    root=tmp_path/'package';root.mkdir()
    for name in ('RUN-RESTORE.ps1','README-离机验收.txt','rehearsal/rehearsal.json',
                 'rehearsal/bundles/initial/completed.json','rehearsal/capacity/prices/completed.json'):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic package fixture')
    for name in (*ENTRYPOINTS,'trade_system/__init__.py','trade_system/v2/__init__.py'):
        path=root/'code'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# synthetic code fixture\n')
    environment=root/'environment';environment.mkdir()
    with zipfile.ZipFile(environment/'base.zip','w') as archive:
        for name in ('python.exe','python312.dll','Lib/encodings/__init__.py'):
            data=b'synthetic base file';archive.writestr('python-collector/'+name,data)
            path=root/'bootstrap'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    wheels=environment/'wheels';wheels.mkdir();blocks=[]
    for name in sorted(CORE_PACKAGES):
        version='0.82.2' if name=='jsonpath' else '1.0'
        path=wheels/(name+'-'+version+'-py3-none-any.whl')
        with zipfile.ZipFile(path,'w') as archive:
            prefix=name+'-'+version+'.dist-info/'
            archive.writestr(prefix+'METADATA','Metadata-Version: 2.1\nName: '+name+'\nVersion: '+version+'\n')
            archive.writestr(prefix+'WHEEL','Wheel-Version: 1.0\nTag: py3-none-any\n')
        blocks.append(name+'=='+version+' --hash=sha256:'+package.digest(path)+'\n')
    for name in LOCKS:(root/'code'/name).write_text(''.join(blocks))
    extra='jsonpath-0.82.2-py3-none-any.whl'
    sha=package.seal_package(root,base_sha=package.digest(root/'environment/base.zip'),jsonpath_name=extra,
        jsonpath_sha=package.digest(wheels/extra),catalog='capacity/prices')
    return root,sha


@pytest.mark.parametrize('damage',['missing','altered','extra','wrong-pin','fake-offsite','path-escape'])
def test_portable_package_requires_exact_material_closure(tmp_path,damage):
    root,pin=fake_package(tmp_path)
    assert package.verify_package(root,pin)['offsite_accepted'] is False
    if damage=='missing':(root/'environment/base.zip').unlink()
    elif damage=='altered':(root/'bootstrap/python.exe').write_bytes(b'changed')
    elif damage=='extra':(root/'extra.txt').write_text('extra')
    elif damage=='wrong-pin':pin='0'*64
    else:
        value=json.loads((root/'package.json').read_text())
        if damage=='fake-offsite':value['offsite_accepted']=True
        else:value['files']['../escape']=value['files'].pop('bootstrap/python.exe')
        (root/'package.json').write_text(json.dumps(value));pin=package.digest(root/'package.json')
    with pytest.raises((ValueError,FileNotFoundError)):package.verify_package(root,pin)


def test_portable_restore_never_overwrites_existing_directory(tmp_path):
    root,pin=fake_package(tmp_path);dest=tmp_path/'existing';dest.mkdir();(dest/'keep').write_text('untouched')
    with pytest.raises(ValueError,match='new separate'):package.restore_package(root,pin,dest)
    assert (dest/'keep').read_text()=='untouched'


def test_environment_failure_keeps_evidence_and_cannot_report_recovery_success(tmp_path,monkeypatch):
    from types import SimpleNamespace
    root,pin=fake_package(tmp_path);dest=tmp_path/'attempt'
    monkeypatch.setattr(package.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='missing offline wheel'))
    with pytest.raises(RuntimeError,match='environment-rebuild'):package.restore_package(root,pin,dest)
    assert 'missing offline wheel' in (dest/'evidence/environment-rebuild.log').read_text()
    assert not (dest/'evidence/completed.json').exists() and not (dest/'rehearsal').exists()


def test_report_rejects_incomplete_or_fake_offsite_acceptance(lab):
    root=captured(lab)
    capacity.archive_table(root,'inputs/databases/market.duckdb','multi_source_kline','restores/first','capacity/prices')
    evidence={'acceptance_scope':scope.SCOPE,'execution_ready':False,
              'recovery':recovery_bundle.verified_restore(root,'restores/first'),
              'capacity':capacity.verify_table(root,'capacity/prices','restores/first'),
              'inventory':capacity.inventory(root,'inputs/databases/market.duckdb')}
    html=offline_report.render(evidence)
    assert '本阶段不适用' in html and '离机实测' in html and 'table-search' in html
    evidence['recovery']['offsite_accepted']=True
    with pytest.raises(ValueError):offline_report.render(evidence)
