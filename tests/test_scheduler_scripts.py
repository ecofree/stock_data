from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_scheduled_runners_use_absolute_project_paths_and_durable_logs():
    close = _read("run_stock_data_daily.ps1")
    once = _read("run_phase_once.ps1")
    watch = _read("run_phase_watch.ps1")

    assert "$IntegratedRunner = Join-Path $Root" in close
    assert "--reports-dir" in close
    assert "scheduled_close_" in close
    assert "DAILY_RUN_FAILED" in close
    assert "audit_p0_five_day_observation.py" not in close
    assert "--collector-contract-sha256" in close
    assert "--collector-contract-sha256" in once
    assert "Push-Location $Root" in close

    assert "$IntegratedRunner = Join-Path $Root" in once
    assert "$DbPath = if ([System.IO.Path]::IsPathRooted($Db))" in once
    assert "scheduled_{0}_{1}.log" in once

    assert "$DbPath = if ([System.IO.Path]::IsPathRooted($Db))" in watch
    assert "scheduled_{0}_watch_{1}.log" in watch
    assert "PHASE_WATCH_STOP" in watch
    assert "PHASE_WATCH_DRAIN" in watch
    assert "MinRunWindowSeconds" in watch
    assert "PHASE_WATCH_COMPLETE" in watch


def test_old_registration_is_physically_removed_and_proposal_preserves_identity():
    installer = _read("install_stock_data_task.ps1")
    assert 'Register-ScheduledTask' not in installer
    assert 'Unregister-ScheduledTask' not in installer
    assert 'New-ScheduledTaskPrincipal' not in installer
    assert 'BaselineInventorySha256' in installer
    assert 'Get-ScheduledTask' not in installer
    assert 'preserve existing triggers' in installer
    assert 'dedicated Limited/Password, never SYSTEM' in installer
    assert 'SystemChanges=0;ProductionCutover=$false' in installer


def _ps(script, *args, executable='powershell.exe'):
    return subprocess.run([executable, '-NoProfile', '-NonInteractive', '-File', str(script), *map(str, args)],
        capture_output=True, text=True, encoding='utf-8' if Path(executable).stem.lower() == 'pwsh' else None, timeout=30,
        env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows scheduler adapter')
@pytest.mark.parametrize('case', ['valid', 'compiled', 'compiled_pwsh', 'missing', 'xml_mismatch', 'enabled_compact', 'hash_mismatch', 'wrong_runtime'])
def test_seven_task_proposal_from_readonly_export(tmp_path, case):
    if case=='compiled_pwsh' and not shutil.which('pwsh.exe'):
        pytest.skip('PowerShell 7 is optional; Windows PowerShell 5.1 remains the deployment target')
    baseline=tmp_path/'baseline';baseline.mkdir()
    names=['Auction','Intraday','DailyClose','SupplementalRetry','QLibResearch','ResearchDaily','MonthlyCompact']
    tasks=[]
    for suffix in names:
        name='StockData-'+suffix
        disabled=suffix in ('ResearchDaily','MonthlyCompact')
        tasks.append({'Name':name, 'State':'Disabled' if disabled else 'Ready',
            'Actions':[{'Execute':'PowerShell.exe','Arguments':'old','WorkingDirectory':None}],
            'Principal':{'UserId':'SYSTEM','LogonType':5,'RunLevel':1},'Triggers':[]})
        (baseline/(name+'.xml')).write_text(
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Principals><Principal><UserId>S-1-5-21-123-1006</UserId><LogonType>Password</LogonType></Principal></Principals>'
            f'<Settings><Enabled>{str(not disabled).lower()}</Enabled></Settings><Triggers/>'
            '<Actions><Exec><Command>PowerShell.exe</Command><Arguments>old</Arguments></Exec></Actions></Task>')
    if case=='compiled_pwsh':tasks=[t for t in tasks if t['Name'].split('-')[-1] in ('Auction','Intraday','DailyClose','MonthlyCompact')]
    if case=='missing':tasks.pop()
    if case=='xml_mismatch':tasks[0]['Actions'][0]['Arguments']='changed'
    if case=='enabled_compact':tasks[-1]['State']='Ready'
    inventory=baseline/'tasks.json';inventory.write_text(json.dumps(tasks))
    contract=tmp_path/'contract.json';contract.write_text(json.dumps({'scope':'transitional_market_collection_only',
        'execution_ready':False,'python':str(tmp_path/'wrong.exe') if case=='wrong_runtime' else sys.executable,
        'database':str(tmp_path/'db.duckdb'),'reports':str(tmp_path/'data')}))
    release=tmp_path/'release';release.mkdir();manifest=release/'research-release.json';manifest.write_text('{"version":"0.3.26"}')
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    output=tmp_path/'proposal.json'
    args=['-BaselineDirectory',baseline,'-BaselineInventorySha256',sha(inventory) if case!='hash_mismatch' else '0'*64,
        '-CollectorContract',contract,'-CollectorContractSha256',sha(contract),'-AdapterPython',sys.executable,
        '-ResearchPython',sys.executable,'-ResearchReleaseDirectory',release,'-ResearchReleaseManifestSha256',sha(manifest),
        '-EnvironmentFile',tmp_path/'protected.env','-Workspace',tmp_path/'workspace','-Output',output]
    if case.startswith('compiled'):args+=['-ResearchStartBoundary','2026-09-18T19:30:00+08:00']
    result=_ps(ROOT/'scripts/install_stock_data_task.ps1',*args)
    if case not in ('valid','compiled','compiled_pwsh'):
        assert result.returncode != 0 and not output.exists()
        return
    assert result.returncode==0,result.stderr
    saved=output.read_bytes();proposal=json.loads(saved)
    assert proposal['SystemChanges']==0 and proposal['ProductionCutover'] is False
    rows={r['Name']:r for r in proposal['Actions']}
    assert len(rows)==len(tasks) and len(proposal['BaselineXmlSha256'])==len(tasks)
    assert len(proposal['AbsentTasks']) == 7-len(tasks)
    if 'StockData-QLibResearch' in rows:
        assert '-Phase supplemental -PublicationTask StockData-ResearchDaily' in rows['StockData-SupplementalRetry']['Arguments']
        assert '-RefreshResearch' in rows['StockData-QLibResearch']['Arguments']
        assert rows['StockData-QLibResearch']['Principal']['RunLevel']=='Limited'
    assert rows['StockData-MonthlyCompact']['Arguments']=='old'
    assert rows['StockData-MonthlyCompact']['Disposition']=='preserve_disabled'
    assert _ps(ROOT/'scripts/install_stock_data_task.ps1',*args).returncode != 0
    assert output.read_bytes()==saved
    if case.startswith('compiled'):
        import xml.etree.ElementTree as ET
        assert proposal['Scope']=='task_handover_engineering_only'
        assert proposal['LiveBackendEnabled'] is False and proposal['AuthenticationVerified'] is False
        assert rows['StockData-MonthlyCompact']['AfterXml']==rows['StockData-MonthlyCompact']['BeforeXml']
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        for name,row in rows.items():
            after=ET.fromstring(row['AfterXml'])
            if name.endswith('MonthlyCompact'):continue
            assert after.find('t:Actions/t:Exec/t:Arguments',ns).text==row['Arguments']
            assert after.find('t:Settings/t:StartWhenAvailable',ns).text=='false'
        assert proposal['Version']=='0.3.26'
        if 'StockData-ResearchDaily' in rows:
            research=ET.fromstring(rows['StockData-ResearchDaily']['AfterXml'])
            assert research.find('t:Principals/t:Principal/t:RunLevel',ns).text=='LeastPrivilege'
            assert research.find('t:Triggers/t:CalendarTrigger/t:StartBoundary',ns).text=='2026-09-18T19:30:00+08:00'
        engine=ROOT/'scripts/deploy_current_tasks.ps1'
        run_engine=lambda *a:_ps(engine,*a,executable='pwsh.exe' if case=='compiled_pwsh' else 'powershell.exe')
        check=run_engine('-Mode','Check','-Plan',output,'-PlanSha256',sha(output))
        assert check.returncode==0,check.stdout+check.stderr
        rehearsal=tmp_path/'rehearsal'
        run=run_engine('-Mode','Rehearse','-Plan',output,'-PlanSha256',sha(output),'-OutputDirectory',rehearsal)
        assert run.returncode==0,run.stdout+run.stderr
        receipt=json.loads((rehearsal/'receipt.json').read_text())
        assert receipt['passed'] is True and receipt['real_authentication_verified'] is False
        assert receipt['scope']=='offline_task_transaction_fault_injection' and receipt['windows_task_changes']==0
        assert receipt['plan_sha256']==sha(output) and receipt['installer_sha256'].lower()==sha(engine)
        results={r['scenario']:r for r in receipt['scenarios']}
        assert len(results)==8 and all(r['passed'] for r in results.values())
        assert results['roundtrip']['fixture_writes']==2*(len(tasks)-1)
        assert results['roundtrip']['password_parameter_fixture_writes']>0
        for failure in ('stage_failure','activation_failure','window_expiry'):
            assert results[failure]['status']=='rolled_back'
        for failure in ('rollback_auth_failure','independent_drift'):
            assert results[failure]['status']=='rollback_incomplete'
        for failure in ('missing_credential','running_before'):
            assert results[failure]['fixture_writes']==0 and not (rehearsal/(failure+'.json')).exists()
        assert run_engine('-Mode','Rehearse','-Plan',output,'-PlanSha256',sha(output),'-OutputDirectory',rehearsal).returncode!=0
        assert run_engine('-Mode','Check','-Plan',output,'-PlanSha256','0'*64).returncode!=0


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows engineering transaction policy')
@pytest.mark.parametrize('mode',['Apply','Rollback'])
def test_current_engine_requires_explicit_local_handover_before_mutation(mode):
    engine=ROOT/'scripts/deploy_current_tasks.ps1'
    result=_ps(engine,'-Mode',mode)
    assert result.returncode!=0 and 'LIVE_DISABLED' in result.stderr
    text=engine.read_text(encoding='utf-8')
    for forbidden in ('Set-ScheduledTask','Start-ScheduledTask','Stop-ScheduledTask',
                      'Set-LocalUser','Enable-LocalUser','Set-Acl','Remove-Item','Stop-Process'):
        assert forbidden not in text


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows explicit window parsing')
def test_current_window_does_not_reuse_expired_or_implicit_authority():
    command=r'''
$ErrorActionPreference='Stop'
. '__ENGINE__' -Mode Library
foreach ($bounds in @(@('2026-09-17T10:00:00+08:00','2026-09-17T11:01:00+08:00'),
 @('2026-09-17T10:00:00','2026-09-17T11:00:00'),
 @('2026-09-16T10:00:00+08:00','2026-09-16T11:00:00+08:00'))) {
 $rejected=$false;try {Assert-WindowBounds $bounds[0] $bounds[1] ([DateTimeOffset]'2026-09-17T10:30:00+08:00')} catch {$rejected=$true}
 if (-not $rejected) {throw 'Unsafe window accepted'}
}
Assert-WindowBounds '2026-09-17T10:00:00+08:00' '2026-09-17T11:00:00+08:00' ([DateTimeOffset]'2026-09-17T10:30:00+08:00')
'''.replace('__ENGINE__',str(ROOT/'scripts/deploy_current_tasks.ps1'))
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows scheduler adapter')
@pytest.mark.parametrize('case,code,requested', [('success',0,True),('degraded',2,True),('closed',0,False),
    ('unknown_calendar',2,False),('lock',3,False),('normalize_failed',2,False),('wrong_receipt',1,False),
    ('running',1,False),('unsafe_task',1,False)])
def test_supplemental_publication_uses_exact_receipt_and_mocked_task(tmp_path,case,code,requested):
    scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'run_phase_once.ps1').write_text(_read('run_phase_once.ps1'))
    (scripts/'run_integrated_daily.py').write_text('# never executed')
    # Only these in-process fixture functions can access a task. No real
    # scheduler, provider, database, account or live runtime is used.
    native=r'''
function Invoke-StockDataProcess {
 param($Executable,$Arguments,$WorkingDirectory)
 $id=$Arguments[[Array]::IndexOf($Arguments,'--run-id')+1]
 $reports=$Arguments[[Array]::IndexOf($Arguments,'--reports-dir')+1]
 $path=Join-Path $reports ('runs\'+$id+'\run.json')
 New-Item -ItemType Directory -Path (Split-Path $path) -Force | Out-Null
 $status=switch ('__CASE__') {'closed' {'skipped_market_closed'} 'unknown_calendar' {'blocked_calendar_unverified'} 'degraded' {'completed_with_degradation'} default {'completed'}}
 $normal=if ('__CASE__' -eq 'normalize_failed') {'degraded'} else {'completed'}
 $receipt=@{run_id=$id;trade_date='2026-09-17';phase='supplemental';scope='transitional_market_collection_only';
 collector_contract_sha256=('a'*64);status=$status;steps=@(@{name='build_normalized_views';status=$normal;return_code=0},@{name='collect_lhb_daily';status='completed';return_code=0})}
 if ('__CASE__' -eq 'wrong_receipt') {$receipt.trade_date='2026-09-16'}
 $receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $path
 [pscustomobject]@{ExitCode=__EXIT__;Stdout='fixture';Stderr=''}
}
function Get-ScheduledTask {
 [pscustomobject]@{State=$(if('__CASE__' -eq 'running'){'Running'}else{'Ready'});Settings=@{Enabled=$true};
 Principal=@{UserId='fixture';LogonType='Password';RunLevel=$(if('__CASE__' -eq 'unsafe_task'){'Highest'}else{'Limited'})};
 Actions=@(@{Arguments='-File "C:\fixture\run_research_daily.ps1"'})}
}
function Start-ScheduledTask {Write-Output 'MOCK_REQUEST_ONLY'}
'''.replace('__CASE__',case).replace('__EXIT__',str(code if code!=1 else 0))
    (scripts/'native_process.ps1').write_text(native)
    result=_ps(scripts/'run_phase_once.ps1','-Python',sys.executable,'-Phase','supplemental',
        '-TradeDate','2026-09-17','-CollectorContract','fixture','-CollectorContractSha256','a'*64,
        '-ReportsDirectory',tmp_path/'reports','-PublicationTask','StockData-ResearchDaily')
    assert result.returncode==code,result.stdout+result.stderr
    assert ('MOCK_REQUEST_ONLY' in result.stdout)==requested
    assert ('publication_verified=false' in result.stdout)==requested


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows research adapter')
@pytest.mark.parametrize('case', ['current','closed','wrong_date','failed','refresh_failed'])
def test_research_schedule_uses_frozen_update_not_legacy_pipeline(tmp_path,case):
    scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'run_research_daily.ps1').write_text(_read('run_research_daily.ps1'))
    workspace=tmp_path/'workspace';workspace.mkdir();(workspace/'workspace-config.json').write_text('{}')
    environment=tmp_path/'provider.env';environment.write_text('')
    native=r'''
function Invoke-StockDataProcess {
 param($Executable,$Arguments,$WorkingDirectory)
 if ($Arguments -contains 'check') {return [pscustomobject]@{ExitCode=0;Stdout='';Stderr=''}}
 if ($Arguments -contains 'market-update') {
 $status=if('__CASE__' -eq 'closed'){'market_closed'}else{'market_published'}
 $date=if('__CASE__' -eq 'wrong_date'){'2026-09-16'}else{'2026-09-17'}
 return [pscustomobject]@{ExitCode=$(if('__CASE__' -eq 'failed'){1}else{0});
 Stdout=(@{status=$status;date=$date;snapshot_id='fixture';provider_requests=0;fits=0}|ConvertTo-Json -Compress);Stderr=''}
 }
 if ($Arguments -contains 'update') {return [pscustomobject]@{ExitCode=$(if('__CASE__' -eq 'refresh_failed'){2}else{0});Stdout='FROZEN_UPDATE_MOCK';Stderr=''}}
 throw 'Unexpected command'
}
'''.replace('__CASE__',case)
    (scripts/'native_process.ps1').write_text(native)
    result=_ps(scripts/'run_research_daily.ps1','-Python',sys.executable,'-Workspace',workspace,
        '-EnvironmentFile',environment,'-RefreshResearch','-ExpectedTradeDate','2026-09-17')
    assert (result.returncode==0)==(case in ('current','closed')),result.stderr
    assert ('FROZEN_UPDATE_MOCK' in result.stdout)==(case in ('current','refresh_failed'))
    assert ('DAILY_WORKSPACE_COMPLETE' in result.stdout)==(case=='current')


def test_retired_scheduled_entrypoints_are_absent():
    for stem in ('run_supplemental_retry','run_qlib_research_daily'):
        for suffix in ('.py','.ps1'):
            assert not (ROOT/'scripts'/(stem+suffix)).exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows parameter binding')
@pytest.mark.parametrize('name',['run_phase_once.ps1','run_phase_watch.ps1','run_stock_data_daily.ps1'])
def test_collectors_bind_db_and_fail_missing_contract_before_side_effects(tmp_path,name):
    result=_ps(ROOT/'scripts'/name,'-Db',tmp_path/'must-not-exist.duckdb')
    assert result.returncode != 0 and 'Explicit collector contract' in result.stderr
    assert 'ParameterNameConflictsWithAlias' not in result.stderr
    assert not (tmp_path/'must-not-exist.duckdb').exists()
