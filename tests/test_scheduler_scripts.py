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
    auction_margin = watch.split('elseif ($Phase -eq "auction") {', 1)[1].split('} else {', 1)[0]
    assert '\n    90\n' in auction_margin
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


def _ps(script, *args, executable=r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'):
    return subprocess.run([executable, '-NoProfile', '-NonInteractive', '-File', str(script), *map(str, args)],
        capture_output=True, text=True, encoding='utf-8' if Path(executable).stem.lower() == 'pwsh' else None, timeout=30,
        env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows scheduler adapter')
@pytest.mark.parametrize('case', ['valid', 'runtime_layout', 'compiled', 'compiled_pwsh', 'missing', 'xml_mismatch', 'enabled_compact', 'hash_mismatch', 'wrong_runtime', 'missing_environment', 'relative_environment'])
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
        'database':str(tmp_path/'db.duckdb'),'reports':str(tmp_path/'data'),
        **({'runtime_root':str(tmp_path/'runtime')} if case=='runtime_layout' else {})}))
    release=tmp_path/'release';release.mkdir();manifest=release/'research-release.json';manifest.write_text('{"version":"0.3.26"}')
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    output=tmp_path/'proposal.json'
    environment=tmp_path/'protected.env'
    if case!='missing_environment':environment.write_text('# synthetic fixture; never read by the proposal generator')
    args=['-BaselineDirectory',baseline,'-BaselineInventorySha256',sha(inventory) if case!='hash_mismatch' else '0'*64,
        '-CollectorContract',contract,'-CollectorContractSha256',sha(contract),'-AdapterPython',sys.executable,
        '-ResearchPython',sys.executable,'-ResearchReleaseDirectory',release,'-ResearchReleaseManifestSha256',sha(manifest),
        '-EnvironmentFile','protected.env' if case=='relative_environment' else environment,'-Workspace',tmp_path/'workspace','-Output',output]
    if case.startswith('compiled'):args+=['-ResearchStartBoundary','2026-09-18T19:30:00+08:00']
    result=_ps(ROOT/'scripts/install_stock_data_task.ps1',*args)
    if case not in ('valid','runtime_layout','compiled','compiled_pwsh'):
        assert result.returncode != 0 and not output.exists()
        return
    assert result.returncode==0,result.stderr
    saved=output.read_bytes();proposal=json.loads(saved)
    assert proposal['SystemChanges']==0 and proposal['ProductionCutover'] is False
    assert Path(proposal['EnvironmentFile'])==environment
    rows={r['Name']:r for r in proposal['Actions']}
    assert len(rows)==len(tasks) and len(proposal['BaselineXmlSha256'])==len(tasks)
    assert len(proposal['AbsentTasks']) == 7-len(tasks)
    for suffix in ('Auction','Intraday','DailyClose','SupplementalRetry'):
        if 'StockData-'+suffix in rows:
            assert f'-EnvironmentFile "{environment}"' in rows['StockData-'+suffix]['Arguments']
            workspace_argument = '-ValuationWorkspace "' + str(tmp_path / 'workspace') + '"'
            assert workspace_argument in rows['StockData-'+suffix]['Arguments']
    if 'StockData-QLibResearch' in rows:
        assert '-Phase supplemental -PublicationTask StockData-ResearchDaily' in rows['StockData-SupplementalRetry']['Arguments']
        assert '-RefreshResearch' not in rows['StockData-QLibResearch']['Arguments']
        assert rows['StockData-QLibResearch']['Principal']['RunLevel']=='Limited'
    if case=='runtime_layout':
        assert all('-RuntimeRoot "'+str(tmp_path/'runtime')+'"' in row['Arguments']
                   for name,row in rows.items() if name!='StockData-MonthlyCompact')
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
        enabled=set()
        for name,row in rows.items():
            after=ET.fromstring(row['AfterXml'])
            if after.find('t:Settings/t:Enabled',ns).text=='true':
                enabled.add(name)
            if name.endswith('MonthlyCompact'):continue
            assert after.find('t:Actions/t:Exec/t:Arguments',ns).text==row['Arguments']
            assert after.find('t:Settings/t:StartWhenAvailable',ns).text=='false'
        assert enabled==set(rows)-{'StockData-QLibResearch','StockData-MonthlyCompact'}
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
        if 'StockData-QLibResearch' in rows:
            # A reviewed activation allowlist cannot be contradicted by the
            # compiled task XML, even when its own file hash is supplied.
            tampered=json.loads(saved)
            qlib=next(row for row in tampered['Actions'] if row['Name']=='StockData-QLibResearch')
            xml=ET.fromstring(qlib['AfterXml'])
            xml.find('t:Settings/t:Enabled',ns).text='true'
            qlib['AfterXml']=ET.tostring(xml,encoding='unicode')
            unsafe=tmp_path/'enabled-retired-task.json'
            unsafe.write_text(json.dumps(tampered),encoding='utf-8')
            rejected=run_engine('-Mode','Check','-Plan',unsafe,'-PlanSha256',sha(unsafe))
            assert rejected.returncode!=0 and 'Compiled task XML mismatch' in rejected.stderr


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
    ('running',1,False),('unsafe_task',1,False),('legacy_empty_key',0,True),
    ('wrong_sha',1,False),('changed_receipt',1,False),('string_readiness',1,False)])
def test_supplemental_publication_uses_exact_receipt_and_mocked_task(tmp_path,case,code,requested):
    scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'run_phase_once.ps1').write_text(_read('run_phase_once.ps1'))
    (scripts/'run_integrated_daily.py').write_text('# never executed')
    # Only these in-process fixture functions can access a task. No real
    # scheduler, provider, database, account or live runtime is used.
    native=r'''
function Invoke-StockDataProcess {
 param($Executable,$Arguments,$WorkingDirectory,$TimeoutSeconds)
 if ($Arguments -contains '-c') {
   $out=& $Executable @Arguments
   return [pscustomobject]@{ExitCode=$LASTEXITCODE;Stdout=($out -join "`n");Stderr=''}
 }
 $id=$Arguments[[Array]::IndexOf($Arguments,'--run-id')+1]
 $reports=$Arguments[[Array]::IndexOf($Arguments,'--reports-dir')+1]
 $path=Join-Path $reports ('runs\'+$id+'\run.json')
 New-Item -ItemType Directory -Path (Split-Path $path) -Force | Out-Null
 $status=switch ('__CASE__') {'closed' {'skipped_market_closed'} 'unknown_calendar' {'blocked_calendar_unverified'} 'degraded' {'completed_with_degradation'} default {'completed'}}
 $normal=if ('__CASE__' -eq 'normalize_failed') {'degraded'} else {'completed'}
 $receipt=@{run_id=$id;trade_date='2026-09-17';phase='supplemental';scope='transitional_market_collection_only';
 collector_contract_sha256=('a'*64);status=$status;
 publication_readiness=@{passed=$true;trade_date='2026-09-17';reason='current_qualified_prices_available'};
 steps=@(@{name='build_normalized_views';status=$normal;return_code=0},@{name='collect_lhb_daily';status='completed';return_code=0})}
 if ('__CASE__' -eq 'wrong_receipt') {$receipt.trade_date='2026-09-16'}
 if ('__CASE__' -eq 'wrong_sha') {$receipt.collector_contract_sha256=('b'*64)}
 if ('__CASE__' -eq 'string_readiness') {$receipt.publication_readiness.passed='true'}
 if ('__CASE__' -eq 'legacy_empty_key') {$receipt.legacy_evidence=@{source_fingerprints=@{''='unknown'};label='unknown source'}}
 $script:savedReceipt=$path
 $receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $path
 [pscustomobject]@{ExitCode=__EXIT__;Stdout='fixture';Stderr=''}
}
function Get-ScheduledTask {
 if ('__CASE__' -eq 'changed_receipt') {Add-Content -LiteralPath $script:savedReceipt -Value ' '}
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
    if case in ('success','degraded'):
        blocked = native.replace('passed=$true;', 'passed=$false;').replace(
            "reason='current_qualified_prices_available'", "reason='stock_reference_unqualified'")
        (scripts/'native_process.ps1').write_text(blocked)
        result=_ps(scripts/'run_phase_once.ps1','-Python',sys.executable,'-Phase','supplemental',
            '-TradeDate','2026-09-17','-CollectorContract','fixture','-CollectorContractSha256','a'*64,
            '-ReportsDirectory',tmp_path/'reports','-PublicationTask','StockData-ResearchDaily')
        assert result.returncode == code and 'MOCK_REQUEST_ONLY' not in result.stdout
        assert 'reason=stock_reference_unqualified' in result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows research adapter')
@pytest.mark.parametrize('case', ['current','closed','wrong_date','failed','retired_refresh'])
def test_research_schedule_publishes_market_and_rejects_retired_model_refresh(tmp_path,case):
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
 if ($Arguments -contains 'update') {throw 'Retired update must never be invoked'}
 throw 'Unexpected command'
}
'''.replace('__CASE__',case)
    (scripts/'native_process.ps1').write_text(native)
    arguments=['-Python',sys.executable,'-Workspace',workspace,
        '-EnvironmentFile',environment,'-ExpectedTradeDate','2026-09-17']
    if case=='retired_refresh':arguments+=['-RefreshResearch']
    result=_ps(scripts/'run_research_daily.ps1',*arguments)
    assert (result.returncode==0)==(case in ('current','closed')),result.stderr
    assert 'FROZEN_UPDATE_MOCK' not in result.stdout
    if case=='retired_refresh':
        assert 'Retired active model refresh' in result.stderr
        assert not (workspace/'scheduled-logs').exists()
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


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows environment inheritance')
@pytest.mark.parametrize('explicit', [True, False])
def test_collector_provider_environment_path_reaches_child_without_copying_contents(tmp_path, monkeypatch, explicit):
    scripts=tmp_path/'scripts';scripts.mkdir()
    for name in ('run_phase_once.ps1','native_process.ps1'):
        (scripts/name).write_text(_read(name))
    child=scripts/'run_integrated_daily.py'
    child.write_text("import os,sys\nprint('PROVIDER_ENV_PATH='+os.environ.get('KPL_ENV_FILE',''))\nprint('RUNTIME_ROOT='+os.environ.get('STOCKDATA_RUNTIME_ROOT',''))\nprint('VALUATION_WORKSPACE='+sys.argv[sys.argv.index('--valuation-workspace')+1])\nsys.exit(3)\n")
    inherited=tmp_path/'inherited.env';inherited.write_text('SYNTHETIC=not-a-credential')
    selected=tmp_path/'selected.env';selected.write_text('SYNTHETIC=not-a-credential')
    monkeypatch.setenv('KPL_ENV_FILE', str(inherited))
    args=['-Python',sys.executable,'-Db',tmp_path/'not-created.duckdb','-Phase','intraday',
          '-CollectorContract','fixture','-CollectorContractSha256','a'*64,'-ReportsDirectory',tmp_path/'reports',
          '-ValuationWorkspace',tmp_path/'valuation-workspace','-RuntimeRoot',tmp_path/'runtime']
    if explicit:args+=['-EnvironmentFile',selected]
    result=_ps(scripts/'run_phase_once.ps1',*args)
    assert result.returncode==3,result.stdout+result.stderr
    assert 'PROVIDER_ENV_PATH='+str(selected if explicit else inherited) in result.stdout
    assert 'RUNTIME_ROOT='+str(tmp_path/'runtime') in result.stdout
    assert 'VALUATION_WORKSPACE='+str(tmp_path/'valuation-workspace') in result.stdout
    assert 'SYNTHETIC=' not in result.stdout+result.stderr
    assert not (tmp_path/'.env').exists() and not (tmp_path/'not-created.duckdb').exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows close entry startup evidence')
def test_close_entry_records_startup_failure_before_any_database_or_guard_access(tmp_path):
    scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'run_stock_data_daily.ps1').write_text(_read('run_stock_data_daily.ps1'))
    (scripts/'native_process.ps1').write_text(_read('native_process.ps1'))
    (scripts/'run_integrated_daily.py').write_text('# not executed')
    reports=tmp_path/'reports';db=tmp_path/'missing.duckdb'
    result=_ps(scripts/'run_stock_data_daily.ps1','-Db',db,'-Python',sys.executable,
               '-CollectorContract','fixture','-CollectorContractSha256','a'*64,'-ReportsDirectory',reports)
    assert result.returncode!=0
    logs=list((reports/'scheduled-logs').glob('scheduled_close_*.log'))
    assert len(logs)==1
    saved=logs[0].read_text(encoding='utf-8-sig')
    assert 'DAILY_RUN_START' in saved and 'DAILY_RUN_FAILED' in saved and 'not found' in saved
    assert not db.exists() and not Path(str(db)+'.pipeline.lock.guard').exists()


def test_close_entry_uses_shared_owner_and_only_qualified_backup_retention():
    # Function behavior and injected copy/compress/qualification failures are
    # exercised in test_pipeline_runtime; this checks the live wrapper wiring.
    close=_read('run_stock_data_daily.ps1')
    assert 'prepare_daily_backup' in close and 'finalize_daily_backup' in close
    assert 'retain_qualified_backups' in close
    assert 'Test-Path -LiteralPath "$DbPath.pipeline.lock"' not in close
    assert 'Remove-Item' not in close and 'Stop-Process' not in close
    assert close.index('$result=Invoke-StockDataProcess') < close.index('finalize_daily_backup')
    assert 'start_latest_at' in close and 'deadline_at' in close


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 5.1 close deadline and calendar')
@pytest.mark.parametrize('calendar_state', ['open', 'closed', 'unverified'])
@pytest.mark.parametrize('inherited', [False, True])
def test_close_ps51_calendar_and_all_deadline_serializers(tmp_path, monkeypatch, calendar_state, inherited):
    scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'run_stock_data_daily.ps1').write_text(_read('run_stock_data_daily.ps1'))
    (scripts/'run_integrated_daily.py').write_text('# injected collector boundary; no market access')
    db=tmp_path/'fixture.duckdb';db.write_bytes(b'isolated serializer fixture')
    # Freeze only external process receipts. The actual PS5 wrapper must traverse
    # preflight, calendar, backup, collection, finalization and retention itself.
    # Separate native-process and pipeline-runtime tests exercise those owners.
    native=r'''function Invoke-StockDataProcess {
 param($Executable,$Arguments,$WorkingDirectory,$DeadlineEpoch,$TimeoutSeconds,$ProgressPath)
 $step=if ($Arguments -contains '--dry-run') {'preflight'}
 elseif ($Arguments[0] -like '*run_integrated_daily.py') {'collection'}
 elseif (($Arguments -join ' ') -like '*trading_session_status*') {'calendar'}
 elseif (($Arguments -join ' ') -like '*observation_windows*') {'window'}
 elseif (($Arguments -join ' ') -like '*prepare_daily_backup*') {'prepare'}
 elseif (($Arguments -join ' ') -like '*finalize_daily_backup*') {'finalize'}
 elseif (($Arguments -join ' ') -like '*retain_qualified_backups*') {'retention'}
 else {throw 'Unexpected fixture process'}
 Add-Content -LiteralPath '__CALLS__' -Value $step
 $body=switch ($step) {
   'calendar' {@{state='__STATE__';reason='explicit synthetic fixture'}}
   'window' {@{deadline_at=[DateTimeOffset]::UtcNow.AddMinutes(10).ToString('o');start_latest_at=[DateTimeOffset]::UtcNow.AddMinutes(7).ToString('o')}}
   'prepare' {@{raw='fixture-raw'}}
   'finalize' {@{status='qualified';qualified_recovery_point=$true;archive='fixture-archive';cleanup_errors=@()}}
   default {@{status='fixture'}}
 }
 if ($step -in @('prepare','finalize','retention')) {
   $number=[double]::Parse($Arguments[-1],[Globalization.CultureInfo]::InvariantCulture)
   if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) {throw 'Invalid serialized deadline'}
 }
 [pscustomobject]@{ExitCode=0;Stdout=($body|ConvertTo-Json -Compress);Stderr=''}
}
'''.replace('__STATE__',calendar_state).replace('__CALLS__',str(tmp_path/'calls'))
    (scripts/'native_process.ps1').write_text(native)
    monkeypatch.delenv('STOCKDATA_PHASE_DEADLINE_EPOCH',raising=False)
    if inherited:
        import time
        monkeypatch.setenv('STOCKDATA_PHASE_DEADLINE_EPOCH',str(time.time()+900))
    result=_ps(scripts/'run_stock_data_daily.ps1','-Db',db,'-Python',sys.executable,
        '-TradeDate','2026-10-09','-CollectorContract','fixture','-CollectorContractSha256','a'*64,
        '-ReportsDirectory',tmp_path/'reports')
    assert (result.returncode==0)==(calendar_state!='unverified'),result.stdout+result.stderr
    calls=(tmp_path/'calls').read_text().splitlines()
    if calendar_state=='open':
        assert calls==['preflight','calendar','window','prepare','collection','finalize','retention']
        assert 'qualified_recovery_point=true' in result.stdout
    else:
        assert calls==['preflight','calendar']
        assert not (tmp_path/'backups').exists()
        assert ('DAILY_RUN_MARKET_CLOSED' in result.stdout)==(calendar_state=='closed')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 5.1 watch exit contract')
@pytest.mark.parametrize('strict,operational', [(False,True),(True,False),(False,False),(True,True)])
def test_watch_ps51_exit_uses_operational_audit_without_overwriting_strict(tmp_path, strict, operational):
    # Execute the actual watch final-audit adapter with a deterministic auditor
    # fixture. Domain identity/window/snapshot checks are exercised separately.
    package=tmp_path/'trade_system';package.mkdir();(package/'__init__.py').write_text('')
    (package/'pipeline_runtime.py').write_text('def load_observation_contract(*a): return {}\ndef all_manifests(*a): return []\n')
    (package/'p0_observation.py').write_text(
        'def audit_phase_windows(*a, operational=False, db_path=None):\n'
        "    assert (db_path=='fixture.duckdb') == operational\n"
        f"    return dict(passed=({operational!r} if operational else {strict!r}), required_window_count=2, qualified_window_count=(2 if operational else 0), evidence_errors=[])\n")
    tail=_read('run_phase_watch.ps1').split('$readResult = ',1)[1]
    prelude=f'''$ErrorActionPreference='Stop'
$Root='{tmp_path}'
$Python='{sys.executable}'
$CollectorContract='fixture';$CollectorContractSha256=('a'*64);$day='2026-10-09'
$Phase='intraday';$ReportsDirectory='{tmp_path}';$DbPath='fixture.duckdb'
$windows=@(1,2);$attempts=2;$successes=2;$deferred=0;$failures=0;$missed=0;$unattempted=0
$prepareAttempts=0;$prepareFailures=0;$EndAt='15:05'
function Write-WatchEvent([string]$Message) {{Write-Output $Message}}
'''
    script=tmp_path/'watch-tail.ps1';script.write_text(prelude+'$readResult = '+tail)
    result=_ps(script)
    assert result.returncode==(0 if operational else 1),result.stdout+result.stderr
    lines=result.stdout.splitlines()
    strict_line=next(line for line in lines if 'PHASE_WATCH_QUALIFICATION' in line)
    operational_line=next(line for line in lines if 'PHASE_WATCH_OPERATIONAL' in line)
    assert f'passed={strict}' in strict_line
    assert f'passed={operational}' in operational_line and f'strict_passed={strict}' in operational_line


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 5.1 isolated engineering chain')
def test_ps51_close_commit_supplemental_handoff_and_real_local_publication(tmp_path, monkeypatch):
    """Synthetic clocks/data/task request; real PS5, owners, DuckDB and publisher."""
    import duckdb
    from trade_system.pipeline_runtime import default_observation_policy
    from trade_system.v2.publisher import read_current
    from trade_system.p0_observation import _publications
    core=os.environ.get('STOCKDATA_TEST_CORE_PYTHON',sys.executable)
    research=os.environ.get('STOCKDATA_TEST_RESEARCH_PYTHON',sys.executable)
    scripts=tmp_path/'scripts';scripts.mkdir()
    for name in ('run_stock_data_daily.ps1','run_phase_once.ps1','run_research_daily.ps1'):
        (scripts/name).write_text(_read(name),encoding='utf-8')
    # Freeze only clocks in this copied helper, retaining actual process logic.
    native=_read('native_process.ps1').replace('[DateTimeOffset]::UtcNow',
        "([DateTimeOffset]'2026-10-09T09:31:00+00:00')")
    package=tmp_path/'trade_system';package.mkdir()
    (package/'__init__.py').write_text('__path__.append('+repr(str(ROOT/'trade_system'))+')\n')
    (package/'pipeline_runtime.py').write_text(
        'exec(compile(open('+repr(str(ROOT/'trade_system/pipeline_runtime.py'))+
        ",encoding='utf-8').read(),"+repr(str(ROOT/'trade_system/pipeline_runtime.py'))+",'exec'))\n"
        'class FixtureClock:\n'
        '    def time(self): return 1791538260.0\n'
        'clock=FixtureClock()\n')
    monkeypatch.setenv('PYTHONPATH',str(tmp_path)+os.pathsep+str(ROOT))
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE','1')
    for name in ('STOCKDATA_PHASE_DEADLINE_EPOCH','STOCKDATA_OBSERVATION_WINDOW_DEADLINE_EPOCH'):
        monkeypatch.delenv(name,raising=False)
    db=tmp_path/'market.duckdb'
    with duckdb.connect(str(db)) as c:
        c.execute('CREATE TABLE fixture_commits(phase VARCHAR,run_id VARCHAR)')
        c.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,is_open INTEGER,cal_date DATE)')
        c.execute("INSERT INTO tushare_trade_cal VALUES ('SSE',1,'2026-10-08'),('SZSE',1,'2026-10-08'),('SSE',1,'2026-10-09'),('SZSE',1,'2026-10-09')")
        c.execute('CREATE TABLE v_kline_daily(trade_date VARCHAR,stock_code VARCHAR,change_pct DOUBLE,provider VARCHAR,adjustment VARCHAR,volume_unit VARCHAR,amount_unit VARCHAR,fetched_at TIMESTAMP,close DOUBLE)')
        c.execute('CREATE TABLE v_default_concept_daily(trade_date DATE,concept_code VARCHAR,concept_name VARCHAR,stock_count INTEGER)')
        c.execute('CREATE TABLE v_default_concept_stock_history(trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,stock_name VARCHAR)')
    contract=tmp_path/'contract.json'
    contract.write_text(json.dumps({'observation_windows':default_observation_policy()}))
    digest=hashlib.sha256(contract.read_bytes()).hexdigest()
    # The response fixture commits under the actual PipelineLock. All database
    # targets are explicitly confined to this fixture; there is no provider API.
    (scripts/'run_integrated_daily.py').write_text(r"""import sys,json
from pathlib import Path
import duckdb
from datetime import datetime
from trade_system.pipeline_runtime import load_observation_contract,observation_windows,PipelineLock
args=sys.argv[1:]
def value(key,default=None): return args[args.index(key)+1] if key in args else default
policy=load_observation_contract(value('--collector-contract'),value('--collector-contract-sha256'))
if '--dry-run' in args: sys.exit(0)
db=Path(value('--db')).resolve();root=Path(__file__).resolve().parents[1]
assert db.parent==root
phase=value('--phase');day=value('--trade-date');run=value('--run-id','fixture_close')
assert day=='2026-10-09'
if phase=='close':
 window=observation_windows(policy,day,phase)[0]
 assert datetime.fromisoformat(window['start_at']) <= datetime.fromisoformat(day+'T17:31:00+08:00') <= datetime.fromisoformat(window['start_latest_at'])
with PipelineLock(db,run):
 with duckdb.connect(str(db)) as con:
  con.execute('INSERT INTO fixture_commits VALUES (?,?)',[phase,run])
  if phase=='close':
   con.execute("INSERT INTO v_kline_daily VALUES ('2026-10-08','000001',1,'fixture','none','shares','yuan','2026-10-08 16:00',10),('2026-10-09','000001',2,'fixture','none','shares','yuan','2026-10-09 17:31',10.2)")
  else: con.execute("UPDATE v_kline_daily SET change_pct=3,close=10.3 WHERE trade_date='2026-10-09'")
  count=con.execute('SELECT count(*) FROM v_kline_daily WHERE trade_date=?',[day]).fetchone()[0]
receipt={'run_id':run,'phase':phase,'trade_date':day,'scope':'transitional_market_collection_only',
 'status':'completed_with_certification_gaps','collector_contract_sha256':value('--collector-contract-sha256'),
 'steps':[{'name':'build_normalized_views','status':'completed','return_code':0}],
 'publication_readiness':{'passed':count==1,'trade_date':day,'reason':'synthetic_committed_price'},
 'legacy_evidence':{'source_fingerprints':{'':'unknown'},'label':'\u5408\u6210\u56de\u6267'},'execution_ready':False}
path=Path(value('--reports-dir'))/'runs'/run/'run.json';path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(receipt,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'committed_rows':count,'run_id':run,'fixture':True}))
""",encoding='utf-8')
    workspace=tmp_path/'workspace';workspace.mkdir()
    (workspace/'workspace-config.json').write_text(json.dumps({'market_database':str(db),'read_only':True}))
    release=tmp_path/'fixture-release';release.mkdir()
    (release/'research-release.json').write_text('{"scope":"synthetic_clock_fixture_not_release_acceptance"}')
    release_sha=hashlib.sha256((release/'research-release.json').read_bytes()).hexdigest()
    (release/'run_research.py').write_text(
        'import sys\nsys.path.insert(0,'+repr(str(ROOT))+')\n'
        'from datetime import datetime\nfrom trade_system.v2 import daily_workspace\n'
        "daily_workspace.now_utc=lambda:datetime.fromisoformat('2026-10-09T12:30:00+00:00')\n"
        'from trade_system.v2.research_product import main\nmain()\n')
    native += r'''
function Get-ScheduledTask {
 [pscustomobject]@{State='Ready';Settings=@{Enabled=$true};Principal=@{UserId='synthetic';LogonType='Password';RunLevel='Limited'};
 Actions=@(@{Arguments='-File "C:\fixture\run_research_daily.ps1"'})}
}
function Start-ScheduledTask {
 Add-Content -LiteralPath '__REQUESTS__' -Value 'synthetic-request'
 & 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -NoProfile -NonInteractive -File '__RESEARCH_PS__' -Python '__PYTHON__' -Workspace '__WORKSPACE__' -ExpectedTradeDate '2026-10-09' -ReleaseDirectory '__RELEASE__' -ReleaseManifestSha256 '__SHA__'
 if ($LASTEXITCODE -ne 0) {throw 'Actual isolated publication failed'}
}
'''.replace('__REQUESTS__',str(tmp_path/'requests')).replace('__RESEARCH_PS__',str(scripts/'run_research_daily.ps1')).replace('__PYTHON__',research).replace('__WORKSPACE__',str(workspace)).replace('__RELEASE__',str(release)).replace('__SHA__',release_sha)
    (scripts/'native_process.ps1').write_text(native,encoding='utf-8')
    args=['-Db',db,'-Python',core,'-TradeDate','2026-10-09','-CollectorContract',contract,
        '-CollectorContractSha256',digest,'-ReportsDirectory',tmp_path/'reports']
    closed=_ps(scripts/'run_stock_data_daily.ps1',*args)
    assert closed.returncode==0,closed.stdout+closed.stderr
    assert 'BACKUP_COMPLETE' in closed.stdout and 'DAILY_RUN_COMPLETE' in closed.stdout
    close_receipts=list((tmp_path/'reports/runs').glob('close_*/run.json'))
    assert len(close_receipts)==1
    close_receipt=close_receipts[0]
    original=close_receipt.read_bytes()
    assert json.loads(original)['run_id']==close_receipt.parent.name
    supplement=_ps(scripts/'run_phase_once.ps1',*args,'-Phase','supplemental','-PublicationTask','StockData-ResearchDaily')
    assert supplement.returncode==0,supplement.stdout+supplement.stderr
    assert 'PUBLICATION_REQUESTED' in supplement.stdout and 'DAILY_WORKSPACE_COMPLETE' in supplement.stdout
    supplemental_receipt=next((tmp_path/'reports/runs').glob('supplemental_*/run.json')).read_bytes()
    assert json.loads(supplemental_receipt)['legacy_evidence']=={'source_fingerprints':{'':'unknown'},'label':'合成回执'}
    assert 'receipt_sha256='+hashlib.sha256(supplemental_receipt).hexdigest() in supplement.stdout
    assert (tmp_path/'requests').read_text().splitlines()==['synthetic-request']
    assert close_receipt.read_bytes()==original
    with duckdb.connect(str(db),read_only=True) as c:
        assert c.execute('SELECT phase FROM fixture_commits ORDER BY rowid').fetchall()==[('close',),('supplemental',)]
    manifest,files=read_current(workspace/'publication')
    desk=json.loads(files['desk.json'])
    publication_receipts=[json.loads(line) for line in supplement.stdout.splitlines()
        if line.strip().startswith('{') and 'market_published' in line]
    assert len(publication_receipts)==1
    publication_receipt=publication_receipts[0]
    assert publication_receipt['publication']==json.loads((workspace/'publication/current.json').read_bytes())
    assert publication_receipt['publication']['run_id']==manifest['run_id']
    assert publication_receipt['publication']['generation']==manifest['generation']
    assert publication_receipt['snapshot_id']==desk['market']['snapshot_id']
    assert publication_receipt['report_id']==desk['report_id']
    assert desk['market']['trade_date']=='2026-10-09' and desk['market']['breadth']['samples']==1
    assert desk['market']['stocks']['000001']['change_pct']==3
    assert not desk['execution_ready']
    research_log=next((workspace/'scheduled-logs').glob('*.log')).read_bytes()
    assert b'\x00' not in research_log
    log_text=research_log.decode('utf-8-sig')
    assert 'No broken requirements found.' in log_text and 'market_published' in log_text
    published,error=_publications(workspace,['2026-10-09'])
    assert not error and published['2026-10-09']['passed'],published
