import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.v2.deployment_probe import write_denied


ROOT = Path(__file__).resolve().parents[1]
@pytest.fixture(scope='module', autouse=True)
def bind_frozen_recovery(frozen_recovery):
    global SCRIPT, HELPER
    SCRIPT = frozen_recovery / 'scripts/deploy_research_cutover.ps1'
    HELPER = frozen_recovery / 'scripts/deploy_research_cutover.ps1'



def daily_probe_fixture(prediction_date=None):
    from trade_system.v2.daily_workspace import empty_projection
    from trade_system.v2.domain import identity
    data=empty_projection()
    market={'trade_date':'2026-09-16','stocks':{'000001':{}},'execution_ready':False,
            'session_state':'calendar_unknown','scope':'read_only_market_review_not_execution'}
    data['market']=dict(market,snapshot_id=identity(market))
    data['prediction_matches_market']=None
    if prediction_date:
        data['prediction']={'date':prediction_date,'predictions':1,'rows':[{}],'prediction_id':'synthetic'}
        data['prediction_matches_market']=prediction_date=='2026-09-16'
        data['research_status']='frozen_evidence' if data['prediction_matches_market'] else 'historical_prediction_not_current_candidates'
    return data


@pytest.mark.parametrize('prediction_date',[None,'2026-09-16','2026-09-15'])
def test_current_probe_separates_market_and_forecast(prediction_date):
    from tools.v2.deployment_probe import product_contract
    result=product_contract(daily_probe_fixture(prediction_date),'2026-09-16')
    assert result['market_date']=='2026-09-16'
    assert result['prediction_date']==prediction_date
    assert result['historical_prediction']==(prediction_date=='2026-09-15')
    assert result['calendar_state']=='calendar_unknown'
    assert result['data_qualification']=='reported_not_upgraded'


@pytest.mark.parametrize('failure',['stale_market','empty_market','tampered_market','future_forecast',
                                    'unmarked_history','wrong_match_flag','execution'])
def test_current_probe_rejects_invalid_daily_contract(failure):
    from tools.v2.deployment_probe import product_contract
    data=daily_probe_fixture('2026-09-15')
    if failure=='stale_market':data['market']['trade_date']='2026-09-15'
    elif failure=='empty_market':data['market']['stocks']={}
    elif failure=='tampered_market':data['market']['snapshot_id']='0'*64
    elif failure=='future_forecast':data['prediction']['date']='2026-09-17'
    elif failure=='unmarked_history':data['research_status']='frozen_evidence'
    elif failure=='wrong_match_flag':data['prediction_matches_market']=True
    elif failure=='execution':data['execution_ready']=True
    with pytest.raises(ValueError):product_contract(data,'2026-09-16')


def test_probe_inspection_checks_actual_database_and_frozen_pointer_without_writes(tmp_path,monkeypatch):
    import json
    from tools.v2.deployment_probe import inspect_product
    from trade_system.v2 import market_workspace, research_product
    database=tmp_path/'source.duckdb';database.write_bytes(b'synthetic read-only database')
    (tmp_path/'workspace-config.json').write_text(json.dumps({'read_only':True,'market_database':str(database)}))
    data=daily_probe_fixture('2026-09-15');calls=[]
    def latest(path,as_of):
        calls.append((path,as_of));return {'trade_date':'2026-09-16'}
    monkeypatch.setattr(market_workspace,'latest_snapshot',latest)
    monkeypatch.setattr(research_product,'saved_projection',lambda p:data)
    monkeypatch.setattr(research_product,'read_prediction',lambda p:data['prediction'])
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert inspect_product(tmp_path,database,'2026-09-16')[1]['historical_prediction']
    assert calls[0][0]==database
    monkeypatch.setattr(research_product,'read_prediction',lambda p:{'prediction_id':'changed'})
    with pytest.raises(ValueError,match='frozen pointer'):inspect_product(tmp_path,database,'2026-09-16')
    monkeypatch.setattr(market_workspace,'latest_snapshot',lambda *a:{'trade_date':'2026-09-15'})
    with pytest.raises(ValueError,match='source database'):inspect_product(tmp_path,database,'2026-09-16')
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}


def test_workspace_launcher_uses_dedicated_runtime_before_any_mutation():
    text=(ROOT/'scripts/start_research_workbench.ps1').read_text()
    assert ".venv\\Scripts\\python.exe" in text
    assert "[string]$Python='D:\\anaconda\\python.exe'" not in text
    assert text.index('& $Python -m pip check') < text.index('if ($Build)')
    assert 'Runtime dependency check failed' in text


def test_diagnostics_preserved_before_rollback_without_secret_export():
    text=SCRIPT.read_text()
    catch=text[text.index("$failure=$_.Exception.Message"):]
    assert catch.index('failure_diagnostic') < catch.index('Restore-Deployment')
    diagnostic=text[text.index('function Get-ProbeDiagnostic'):text.index('function Get-BatchRightsDiagnostic')]
    assert 'EventID=4625' in diagnostic and 'TargetUserName' in diagnostic
    assert 'HasRun=' in diagnostic and '267011' in diagnostic
    for forbidden in ('Export-ScheduledTask','plainPassword','research.env','Set-ScheduledTask','Enable-LocalUser'):
        assert forbidden not in diagnostic
    rights=text[text.index('function Get-BatchRightsDiagnostic'):text.index('function Assert-AccountParameters')]
    assert '/export /cfg' in rights and '/areas USER_RIGHTS' in rights
    assert not any(' /configure ' in line for line in rights.splitlines() if not line.strip().startswith('#'))
    assert 'ReadAllLines($export)' in rights
    assert text.index('    Assert-BatchLogon\n') < text.index('    Start-ScheduledTask -TaskName $taskName')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows deployment policy preflight')
def test_batch_policy_preflight_rejects_missing_right_and_deny_override():
    command=r'''
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in @('Assert-BatchLogon','Resolve-BatchPolicySid')},$true) | ForEach-Object {Invoke-Expression $_.Extent.Text}
$script:policy=@('SeBatchLogonRight = *S-1-5-32-544')
function Get-BatchRightsDiagnostic {return @{Sid='S-1-5-21-1-2-3-1006';DirectLocalGroups=@(@{Sid='S-1-5-32-545'});BatchPolicies=$script:policy}}
function Reject {$rejected=$false;try {Assert-BatchLogon} catch {$rejected=$true};if (-not $rejected) {throw 'Unqualified policy accepted'}}
Reject
$script:policy=@('SeBatchLogonRight = *S-1-5-21-1-2-3-1006');Assert-BatchLogon
foreach ($sid in @('S-1-5-21-1-2-3-1006','S-1-5-32-545','S-1-5-113','S-1-1-0','S-1-5-11')) {
 $script:policy=@('SeBatchLogonRight = *S-1-5-21-1-2-3-1006',('SeDenyBatchLogonRight = *'+$sid));Reject
}
'''.replace('__SCRIPT__',str(SCRIPT))
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native policy name resolution')
@pytest.mark.parametrize('identity_form', ['bare_sid', 'star_sid', 'qualified_name', 'unqualified_name'])
def test_batch_policy_resolves_native_names_and_sids_without_weakening_denies(identity_form):
    # A built-in identity exercises the real Windows resolver without requiring
    # the deployment account, administrator rights, or any policy modifications.
    command=r'''
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in @('Assert-BatchLogon','Resolve-BatchPolicySid')},$true) | ForEach-Object {Invoke-Expression $_.Extent.Text}
$script:identitySid='S-1-5-32-545'
$qualified=([Security.Principal.SecurityIdentifier]::new($script:identitySid)).Translate([Security.Principal.NTAccount]).Value
$forms=@{bare_sid=$script:identitySid;star_sid=('*'+$script:identitySid);qualified_name=$qualified;unqualified_name=$qualified.Split('\')[-1]}
$identity=$forms['__FORM__']
function Get-BatchRightsDiagnostic {return @{Sid=$script:identitySid;DirectLocalGroups=@();BatchPolicies=$script:policy}}
function Reject {$rejected=$false;try {Assert-BatchLogon} catch {$rejected=$true};if (-not $rejected) {throw 'Unqualified policy accepted'}}
$script:policy=@(('SeBatchLogonRight =  '+$identity+' ,*S-1-5-32-544,*S-1-5-32-551,*S-1-5-32-559,*S-1-5-32-568'),'SeDenyBatchLogonRight = ')
Assert-BatchLogon
$script:policy+=('SeDenyBatchLogonRight = '+$identity);Reject
$everyone=([Security.Principal.SecurityIdentifier]::new('S-1-1-0')).Translate([Security.Principal.NTAccount]).Value
$script:policy=@(('SeBatchLogonRight = '+$identity),('SeDenyBatchLogonRight = '+$everyone));Reject
$script:policy=@(('SeBatchLogonRight = '+$identity))
$script:identitySid='S-1-5-21-1-2-3-1006';Reject
'''.replace('__SCRIPT__',str(SCRIPT)).replace('__FORM__',identity_form)
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native policy name resolution')
@pytest.mark.parametrize('policy_side', ['SeBatchLogonRight', 'SeDenyBatchLogonRight'])
def test_batch_policy_unknown_or_malformed_identity_fails_closed(policy_side):
    command=r'''
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in @('Assert-BatchLogon','Resolve-BatchPolicySid')},$true) | ForEach-Object {Invoke-Expression $_.Extent.Text}
function Get-BatchRightsDiagnostic {return @{Sid='S-1-5-21-1-2-3-1006';DirectLocalGroups=@();BatchPolicies=$script:policy}}
$unknown=$env:COMPUTERNAME+'\CodexMissingPolicy_'+[Guid]::NewGuid().ToString('N')
foreach ($invalid in @('*not_a_sid','*','S-1-invalid',' ',$unknown)) {
 $script:policy=@('SeBatchLogonRight = *S-1-5-21-1-2-3-1006',('__SIDE__ = *S-1-5-21-1-2-3-1006,'+$invalid))
 $message='';try {Assert-BatchLogon} catch {$message=$_.Exception.Message}
 if ($message -notmatch 'policy identity') {throw 'Unknown or malformed policy identity was not explicitly rejected'}
}
'''.replace('__SCRIPT__',str(SCRIPT)).replace('__SIDE__',policy_side)
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_probe_never_modifies_a_file_when_write_is_unexpectedly_allowed(tmp_path):
    path = tmp_path / 'source'; path.write_bytes(b'protected original')
    assert write_denied(path) is False
    assert path.read_bytes() == b'protected original'


def test_probe_only_accepts_access_denied_not_sharing_violations(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        exc = PermissionError('denied'); exc.winerror = 5; raise exc
    monkeypatch.setattr(Path, 'open', denied)
    assert write_denied(tmp_path / 'source') is True
    def sharing(*args, **kwargs):
        exc = PermissionError('busy'); exc.winerror = 32; raise exc
    monkeypatch.setattr(Path, 'open', sharing)
    assert write_denied(tmp_path / 'source') is False


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer syntax')
def test_powershell_installer_parses_and_window_is_exact():
    command = r'''
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
if ($errors.Count) { $errors | ForEach-Object { Write-Output $_.Message };exit 1 }
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-Window'},$true)
Invoke-Expression $fn.Extent.Text
function Read-MaintenancePermit {return $script:testPermit}
$script:testPermit=@{started_utc='2026-09-13T23:29:16+08:00';expires_utc='2026-09-14T00:29:16+08:00'}
foreach ($stamp in @('2026-09-13T19:00:00+08:00','2026-09-13T23:29:15+08:00','2026-09-14T00:29:16+08:00','2026-09-14T23:29:16+08:00')) {
  $rejected=$false;try {Assert-Window ([DateTimeOffset]::Parse($stamp))} catch {$rejected=$true}
  if (-not $rejected) {exit 2}
}
Assert-Window ([DateTimeOffset]::Parse('2026-09-13T23:29:16+08:00'))
Assert-Window ([DateTimeOffset]::Parse('2026-09-14T00:00:00+08:00'))
Assert-Window ([DateTimeOffset]::Parse('2026-09-14T00:29:15+08:00'))
$script:testPermit.expires_utc='2026-09-14T01:29:16+08:00'
$rejected=$false;try {Assert-Window ([DateTimeOffset]::Parse('2026-09-14T00:00:00+08:00'))} catch {$rejected=$true}
if (-not $rejected) {exit 3}
Write-Output 'syntax_and_window_pass'
'''.replace('__SCRIPT__', str(SCRIPT))
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True,
                            text=True, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'syntax_and_window_pass' in result.stdout


def test_installer_has_no_destructive_cleanup_or_system_principal_fallback():
    text = SCRIPT.read_text()
    for forbidden in ('Stop-Process', 'Stop-ScheduledTask', 'Remove-LocalUser', 'Remove-Item', '-ExecutionPolicy Bypass', '-UserId "SYSTEM"'):
        assert forbidden not in text
    assert "Assert-Admin; Assert-Window" in text
    assert "-UserMayNotChangePassword" in text
    assert "-RunLevel Limited" in text
    assert "$trigger.StartBoundary='2026-09-14T18:30:00+08:00'" in text
    assert 'Assert-Hash $backup $entry.before_sha256' in text
    assert 'post-apply artifact audit failed' in text.lower()


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows account parameter validation')
def test_account_parameters_checked_without_installing_or_creating_account():
    command = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
if ($errors.Count) {exit 1}
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-AccountParameters'},$true)
Invoke-Expression $fn.Extent.Text
Assert-AccountParameters -Name 'StockDataResearch' -Description ('x'*48)
foreach ($description in @('',('x'*49),'StockData read-only research publisher; no broker authority')) {
  $rejected=$false
  try {Assert-AccountParameters -Name 'StockDataResearch' -Description $description} catch {$rejected=$true}
  if (-not $rejected) {exit 2}
}
$rejected=$false
try {Assert-AccountParameters -Name ('x'*21) -Description 'valid'} catch {$rejected=$true}
if (-not $rejected) {exit 3}
& '__SCRIPT__' -Mode ValidateParameters
'''.replace('__SCRIPT__', str(SCRIPT))
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True,
                            text=True, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"ParameterValidation":  "PASS"' in result.stdout
    assert '"Changes":  0' in result.stdout


def test_parameter_preflight_precedes_every_deployment_side_effect():
    text = SCRIPT.read_text()
    validation = text.index("if ($Mode -ne 'Rollback') { Assert-AccountParameters")
    assert validation < text.index('foreach ($path in @($repo,$live,$runtime')
    assert validation < text.index('Lock-NewDirectory $runtime; Lock-NewDirectory $stateDir')
    assert '-Description $userDescription -AccountNeverExpires' in text
    assert "if (Test-Path -LiteralPath $runtime) { throw" in text


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows recovery validation')
def test_recovery_rejects_partial_installation_and_unrestored_state():
    command = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-RecoveryState'},$true)
Invoke-Expression $fn.Extent.Text
$runtime='D:\accio\stock-data-runtime';$failureArchive=$runtime+'.failed-20260913-164649'
$stateDir=Join-Path $runtime 'deployment-20260913';$journalPath=Join-Path $stateDir 'journal.json'
$bundle='D:\synthetic-bundle';$hotfixHash='synthetic';$live='D:\synthetic-live'
$userName='StockDataResearch';$taskName='StockData-ResearchDaily'
$script:accountExists=$false;$script:badHash=$false;$script:badAcl=$false;$script:changedTasks=$false
$script:files=[ordered]@{}
foreach ($n in @('trade_system/review_web.py','scripts/audit_daily_review_artifact.py','scripts/check_kpl_connectivity.py')) {$script:files[$n]=@{before_sha256='before';after_sha256='after'}}
$script:tasks=[ordered]@{'StockData-Auction'='a';'StockData-Intraday'='b';'StockData-DailyClose'='c';'StockData-MonthlyCompact'='d'}
$script:saved=[ordered]@{status='rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained';user_created=$false;sid='';installer_sha256='8924c867edadf56f86da7c192560bd5340cda2ccc253d81e03b79fff1b91a16e';acls=@{};files=$script:files;old_tasks=$script:tasks}
function Assert-PlainPath($p) {}
function Test-Path {param($LiteralPath,$PathType) return $LiteralPath -ne $failureArchive}
function Get-Content {param($LiteralPath,[switch]$Raw) if ($LiteralPath -eq $journalPath) {return ($script:saved | ConvertTo-Json -Depth 8)}; return (@{files=$script:files} | ConvertTo-Json -Depth 8)}
function Get-LocalUser {param($Name,$ErrorAction) if ($script:accountExists) {return @{Name=$Name}}}
function Get-ScheduledTask {param($TaskName,$ErrorAction) if ($TaskName -eq 'StockData-MonthlyCompact') {return @{State='Disabled'}}}
function Assert-Hash($Path,$Expected) {if ($script:badHash) {throw 'hash mismatch'}}
function Old-Tasks {if ($script:changedTasks) {return [ordered]@{'unexpected'='task'}};return $script:tasks}
function Get-Acl {
 param($LiteralPath)
 $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false)
 foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))}
 if ($script:badAcl) {$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-1-0'),'Read','Allow'))}
 return $acl
}
function Expect-Reject {
 $rejected=$false;try {Assert-RecoveryState} catch {$rejected=$true}
 if (-not $rejected) {throw 'Unsafe recovery accepted'}
}
Assert-RecoveryState
$script:saved.user_created=$true;Expect-Reject;$script:saved.user_created=$false
$script:saved.acls=@{'D:\somewhere'='recorded'};Expect-Reject;$script:saved.acls=@{}
$script:saved.status='preparing';Expect-Reject;$script:saved.status='rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained'
$script:accountExists=$true;Expect-Reject;$script:accountExists=$false
$script:badHash=$true;Expect-Reject;$script:badHash=$false
$script:badAcl=$true;Expect-Reject;$script:badAcl=$false
$script:changedTasks=$true;Expect-Reject;$script:changedTasks=$false
$failureArchive='D:\unapproved';Expect-Reject
Write-Output 'recovery_rejection_contract_pass'
'''.replace('__SCRIPT__', str(SCRIPT))
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True,
                            text=True, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'recovery_rejection_contract_pass' in result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows archive preservation')
def test_archive_fingerprint_survives_rename_and_detects_changes(tmp_path):
    source=tmp_path/'original';source.mkdir()
    (source/'nested').mkdir();(source/'nested'/'receipt.json').write_text('synthetic receipt')
    command = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Recovery-TreeFingerprint'},$true)
Invoke-Expression $fn.Extent.Text
$before=Recovery-TreeFingerprint '__SOURCE__'
Rename-Item -LiteralPath '__SOURCE__' -NewName 'archived'
$after=Recovery-TreeFingerprint '__DEST__'
if ($before -cne $after) {throw 'Rename changed evidence or ACL'}
[IO.File]::WriteAllText((Join-Path '__DEST__' 'nested/receipt.json'),'synthetic changed receipt')
if ($before -ceq (Recovery-TreeFingerprint '__DEST__')) {throw 'Content change went undetected'}
Write-Output 'archive_preservation_pass'
'''.replace('__SCRIPT__',str(SCRIPT)).replace('__SOURCE__',str(source)).replace('__DEST__',str(tmp_path/'archived'))
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True,
                            text=True, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path/'archived/nested/receipt.json').read_text() == 'synthetic changed receipt'
    assert not source.exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows sequential deployment launcher')
@pytest.mark.parametrize('failed_step,expected', [
    ('StartMaintenance',['StartMaintenance']),
    ('ArchiveFailure',['StartMaintenance','ArchiveFailure']),
    ('Check',['StartMaintenance','ArchiveFailure','Check']),
    ('Apply',['StartMaintenance','ArchiveFailure','Check','Apply']),
    ('none',['StartMaintenance','ArchiveFailure','Check','Apply']),
])
def test_retry_launcher_stops_on_every_failure_without_running_real_installer(tmp_path, failed_step, expected):
    fixture=tmp_path/'synthetic runner with spaces';fixture.mkdir()
    runner=fixture/'retry_research_cutover.ps1'
    runner.write_bytes((SCRIPT.parent/'retry_research_cutover.ps1').read_bytes())
    # The launcher resolves its installer beside itself. Only this synthetic
    # script runs; it records modes, never touches accounts/tasks/live paths.
    (fixture/'deploy_research_cutover.ps1').write_text(
        "param([string]$Mode)\n"
        "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'calls.txt') -Value $Mode\n"
        f"if ($Mode -eq '{failed_step}') {{exit 7}}\nexit 0\n")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(runner)],
                          capture_output=True,text=True,timeout=30,
                          env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert (fixture/'calls.txt').read_text().splitlines()==expected
    assert (result.returncode==0)==(failed_step=='none'),result.stdout+result.stderr
    assert ('RETRY_SEQUENCE_COMPLETE' in result.stdout)==(failed_step=='none')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows one-time maintenance contract')
def test_actual_confirmation_cancellation_and_nonrenewal_in_isolated_directory(tmp_path):
    command = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
foreach ($name in @('Start-OneTimeMaintenance','Assert-Window')) {
 $fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 Invoke-Expression $fn.Extent.Text
}
$maintenanceDir='__TEMP__\permit-state';$maintenancePermit=Join-Path $maintenanceDir 'permit.json'
$live='__TEMP__';$workspace='__TEMP__';$package='synthetic';$zipHash='synthetic-package'
$guards=[Collections.Generic.List[IO.FileStream]]::new()
$script:answer='CANCEL';$script:prompts=0;$script:scheduleChecks=0;$script:rejectSchedule=$false
function Assert-Admin {}
function Assert-Idle {}
function Assert-MaintenanceHostTimezone {}
function Assert-PlainPath($Path) {}
function Assert-RecoveryState {}
function Assert-ScheduleClear($Start,$End) {$script:scheduleChecks++;if ($script:rejectSchedule) {throw 'synthetic schedule conflict'}}
function Hold-Guard($Path) {}
function Assert-Hash($Path,$Expected) {}
function Get-FileHash {param($LiteralPath,$Algorithm) return @{Hash='synthetic-installer'}}
function Lock-NewDirectory($Path) {[IO.Directory]::CreateDirectory($Path) | Out-Null}
function Read-MaintenancePermit {return (Get-Content -LiteralPath $maintenancePermit -Raw | ConvertFrom-Json)}
function Read-Host($Prompt) {$script:prompts++;return $script:answer}
$rejected=$false;try {Start-OneTimeMaintenance} catch {$rejected=$true}
if (-not $rejected -or (Test-Path -LiteralPath $maintenanceDir)) {throw 'Cancellation created a permit'}
$script:answer='START';$script:rejectSchedule=$true
$rejected=$false;try {Start-OneTimeMaintenance} catch {$rejected=$true}
if (-not $rejected -or $script:prompts -ne 1 -or (Test-Path -LiteralPath $maintenanceDir)) {throw 'Conflict did not stop before prompt/write'}
$script:rejectSchedule=$false
Start-OneTimeMaintenance
$before=[IO.File]::ReadAllText($maintenancePermit)
$permit=Read-MaintenancePermit
if (([DateTimeOffset]::Parse($permit.expires_utc)-[DateTimeOffset]::Parse($permit.started_utc)).TotalSeconds -ne 3600) {throw 'Not one hour'}
$rejected=$false;try {Start-OneTimeMaintenance} catch {$rejected=$true}
if (-not $rejected -or $before -cne [IO.File]::ReadAllText($maintenancePermit) -or $script:prompts -ne 2) {throw 'Rerun renewed or reprompted'}
Write-Output 'one_time_confirmation_contract_pass'
'''.replace('__SCRIPT__',str(SCRIPT)).replace('__TEMP__',str(tmp_path))
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=30,
                          env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode==0,result.stdout+result.stderr
    assert 'one_time_confirmation_contract_pass' in result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows next-run conflict contract')
def test_maintenance_rejects_running_due_unknown_or_stale_tasks():
    command = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-ScheduleClear'},$true)
Invoke-Expression $fn.Extent.Text
$start=[DateTimeOffset]::Parse('2026-09-14T06:00:00+08:00');$end=$start.AddMinutes(60)
$script:state='Ready';$script:next=[DateTime]::Parse('2026-09-14T07:00:01')
function Get-ScheduledTask {param($TaskName) return @{TaskName='SYNTHETIC';TaskPath='\';State=$script:state}}
function Get-ScheduledTaskInfo {param($TaskName,$TaskPath) return @{NextRunTime=$script:next}}
function Expect-Rejected {$rejected=$false;try {Assert-ScheduleClear $start $end} catch {$rejected=$true};if (-not $rejected) {throw 'Conflict allowed'}}
Assert-ScheduleClear $start $end
$script:state='Running';Expect-Rejected;$script:state='Ready'
foreach ($when in @('2026-09-14T07:00:00','2026-09-14T06:30:00','2026-09-14T05:59:59','1601-01-01T00:00:00')) {$script:next=[DateTime]::Parse($when);Expect-Rejected}
$script:state='Disabled';Assert-ScheduleClear $start $end
Write-Output 'next_run_conflict_contract_pass'
'''.replace('__SCRIPT__',str(SCRIPT))
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=30,
                          env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode==0,result.stdout+result.stderr
    assert 'next_run_conflict_contract_pass' in result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows private permit binding')
def test_private_permit_rejects_changed_source_package_scope_and_permissions():
    command = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Read-MaintenancePermit'},$true)
Invoke-Expression $fn.Extent.Text
$maintenanceDir='D:\SYNTHETIC';$maintenancePermit=Join-Path $maintenanceDir 'permit.json';$zipHash='package'
$script:exists=$true;$script:private=$true;$script:unexpectedAllow=$false
$script:permit=@{schema=1;scope='approved_0310_recovery_and_readonly_deployment_once';installer_sha256='source';package_sha256='package'}
function Assert-PlainPath($Path) {}
function Test-Path {param($LiteralPath,$PathType) return $script:exists}
function Get-Content {param($LiteralPath,[switch]$Raw) return ($script:permit | ConvertTo-Json)}
function Get-FileHash {param($LiteralPath,$Algorithm) return @{Hash='source'}}
function Get-Acl {
 param($LiteralPath)
 $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($script:private,$false)
 foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))}
 if ($script:unexpectedAllow) {$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-1-0'),'Write','Allow'))}
 return $acl
}
function Expect-Rejected {$rejected=$false;try {Read-MaintenancePermit | Out-Null} catch {$rejected=$true};if (-not $rejected) {throw 'Invalid permit accepted'}}
Read-MaintenancePermit | Out-Null
$script:permit.installer_sha256='changed';Expect-Rejected;$script:permit.installer_sha256='source'
$script:permit.package_sha256='changed';Expect-Rejected;$script:permit.package_sha256='package'
$script:permit.scope='unknown';Expect-Rejected;$script:permit.scope='approved_0310_recovery_and_readonly_deployment_once'
$script:exists=$false;Expect-Rejected;$script:exists=$true
$script:private=$false;Expect-Rejected;$script:private=$true
$script:unexpectedAllow=$true;Expect-Rejected
Write-Output 'private_permit_binding_pass'
'''.replace('__SCRIPT__',str(SCRIPT))
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=30,
                          env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode==0,result.stdout+result.stderr
    assert 'private_permit_binding_pass' in result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows byte-range lock protocol')
def test_powershell_guard_conflicts_with_the_real_python_guard(tmp_path):
    guard = tmp_path / 'existing.guard'; guard.touch()
    command = r'''
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
foreach ($name in @('Assert-PlainPath','Hold-Guard')) {
 $fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 Invoke-Expression $fn.Extent.Text
}
$guards=[Collections.Generic.List[IO.FileStream]]::new()
Hold-Guard '__GUARD__'
try {
 & '__PYTHON__' -B -c "from trade_system.file_lock import FileLock,FileLockBusy; import sys; p=sys.argv[1];`ntry:`n with FileLock(p): pass`nexcept FileLockBusy: sys.exit(3)" '__GUARD__'
 if ($LASTEXITCODE -ne 3) {exit 4}
} finally {foreach ($g in $guards) {$g.Dispose()}}
& '__PYTHON__' -B -c "from trade_system.file_lock import FileLock; import sys;`nwith FileLock(sys.argv[1]): pass" '__GUARD__'
exit $LASTEXITCODE
'''.replace('__SCRIPT__', str(SCRIPT)).replace('__GUARD__', str(guard)).replace('__PYTHON__', sys.executable)
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True,
                            text=True, cwd=ROOT, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert guard.exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows directory access probe')
def test_directory_probe_opens_existing_handle_without_creating_a_note(tmp_path):
    from tools.v2.deployment_probe import directory_write_denied
    assert directory_write_denied(tmp_path) is False
    assert list(tmp_path.iterdir()) == []
