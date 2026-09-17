"""Isolated Windows recovery tests; no real account, task, policy or ACL changes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/recover_research_probe.ps1'
HELPER=ROOT/'scripts/deploy_research_cutover.ps1'


def run_ps(code, tmp_path):
    bootstrap=r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
foreach ($source in @('__HELPER__','__SCRIPT__')) {
 $tokens=$null;$errors=$null
 $ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
 if ($errors.Count) {throw ($errors | Out-String)}
 foreach ($fn in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)) {Invoke-Expression $fn.Extent.Text}
}
$testRoot='__TEMP__'
function Require($condition,[string]$message) {if (-not $condition) {throw $message}}
function Reject([scriptblock]$action) {$blocked=$false;try {& $action} catch {$blocked=$true};Require $blocked 'unsafe input accepted'}
'''.replace('__HELPER__',str(HELPER)).replace('__SCRIPT__',str(SCRIPT)).replace('__TEMP__',str(tmp_path))
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',bootstrap+code],
                          capture_output=True,text=True,timeout=45,
                          env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'})
    assert result.returncode==0,result.stdout+result.stderr
    return result.stdout


def test_recovery_scope_is_offline_and_does_not_recreate_credentials():
    text='\n'.join(line for line in SCRIPT.read_text().splitlines() if not line.strip().startswith('#'))
    for forbidden in ('New-LocalUser','Set-LocalUser','Register-ScheduledTask','New-ScheduledTaskTrigger',
                      'Remove-Item','Remove-LocalUser','Stop-Process','Stop-ScheduledTask',
                      '-ExecutionPolicy Bypass','-Password ', 'Start-OneTimeMaintenance'):
        assert forbidden not in text
    assert "[string]$Mode='Check'" in text
    assert "try {Restore-Probe} catch" in text
    assert 'Test-ProbePackage;Assert-ScheduleClear' in text
    assert text.index("$confirmation -cne 'START'")<text.index('Lock-NewDirectory $probeAttempt')
    assert "'offline_probe_restored_account_and_task_disabled'" in text
    assert 'Assert-RestorePath $path' in text
    assert "'rollback_needs_inspection'" in text


pytestmark=pytest.mark.skipif(sys.platform!='win32',reason='Windows isolated installer behavior')


def test_load_library_has_no_operational_side_effects(tmp_path):
    code=r'''
function Get-ScheduledTask {throw 'task queried while loading definitions'}
function Get-LocalUser {throw 'user queried while loading definitions'}
function New-Item {throw 'file created while loading definitions'}
. '__HELPER__' -Mode LoadLibrary
Require ($null -ne (Get-Command Assert-Hash -ErrorAction Stop)) 'library absent'
'''.replace('__HELPER__',str(HELPER))
    run_ps(code,tmp_path)


IDENTITY=r'''
$userName='StockDataResearch';$expectedSid='S-1-5-21-1-2-3-1006'
$runtime=Join-Path $testRoot 'runtime';$expectedExecute='powershell.exe'
$originalArguments='original-offline-probe';$probeArguments='new-offline-probe'
$script:account=[pscustomobject]@{SID=[pscustomobject]@{Value=$expectedSid};Enabled=$false}
$script:task=[pscustomobject]@{Principal=[pscustomobject]@{UserId=$userName;LogonType='Password';RunLevel='Limited'};State='Disabled';Settings=[pscustomobject]@{Enabled=$false};Triggers=$null;Actions=@([pscustomobject]@{Execute=$expectedExecute;WorkingDirectory=$runtime;Arguments=$originalArguments})}
$taskName='synthetic-only'
function Get-LocalUser {return $script:account}
function Get-ScheduledTask {return $script:task}
'''


def test_retained_identity_rejects_drift_and_accepts_null_trigger_array(tmp_path):
    run_ps(IDENTITY+r'''
$null=Assert-ProbeIdentity
$script:account.Enabled=$true;Reject {Assert-ProbeIdentity};$script:account.Enabled=$false
$script:task.Settings.Enabled=$true;Reject {Assert-ProbeIdentity};$script:task.Settings.Enabled=$false
$script:task.Triggers=@('unexpected');Reject {Assert-ProbeIdentity};$script:task.Triggers=$null
$script:task.Principal.RunLevel='Highest';Reject {Assert-ProbeIdentity};$script:task.Principal.RunLevel='Limited'
$script:task.Principal.LogonType='S4U';Reject {Assert-ProbeIdentity};$script:task.Principal.LogonType='Password'
$script:account.SID.Value='unrelated';Reject {Assert-ProbeIdentity};$script:account.SID.Value=$expectedSid
$script:task.Actions[0].Arguments='unrelated';Reject {Assert-ProbeIdentity};$script:task.Actions[0].Arguments=$probeArguments
$script:account.Enabled=$true;$script:task.State='Ready';$script:task.Settings.Enabled=$true
$null=Assert-ProbeIdentity $false
''',tmp_path)


@pytest.mark.parametrize('failure', ['extract','grant','start','bad_receipt','none'])
def test_probe_failures_restore_without_triggers_or_live_objects(tmp_path,failure):
    code=IDENTITY+r'''
$script:trace=[Collections.Generic.List[string]]::new()
$repo=$testRoot;$live=$testRoot;$workspace=$testRoot;$release=Join-Path $testRoot 'release'
$probeAttempt=$testRoot;$probeScripts=Join-Path $testRoot 'scripts';$probeRoot=Join-Path $testRoot 'results'
$probeLauncher=Join-Path $probeScripts 'probe.ps1';$environment=Join-Path $testRoot 'environment';$python='python.exe'
$script:researchSid=[Security.Principal.SecurityIdentifier]::new($expectedSid)
$script:journal=[ordered]@{sid=$expectedSid;files=[ordered]@{};acls=[ordered]@{};nonce='synthetic';original_task_xml='original';probe_verified=$false;status='prepared'}
$script:failure='__FAILURE__'
function Assert-ProbeWindow {}
function Test-ProbePackage {if ($script:failure -eq 'extract') {throw 'synthetic extract failure'}}
function Grant-Access {if ($script:failure -eq 'grant') {throw 'synthetic grant failure'}}
function Assert-BatchLogon {}
function Assert-OldTasks {}
function Copy-Item {} # No real source/credential copies in a state-machine test.
function Save-Journal {}
function New-ScheduledTaskAction {param($Execute,$Argument,$WorkingDirectory);return [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory}}
function Set-ScheduledTask {
 param($Action)
 if ($script:task.Actions[0].Arguments -eq $Action.Arguments) {throw 'Redundant password-task update rejected'}
 $script:trace.Add('set-action');$script:task.Actions=@($Action)
}
function Enable-LocalUser {$script:trace.Add('enable-account');$script:account.Enabled=$true}
function Disable-LocalUser {$script:trace.Add('disable-account');$script:account.Enabled=$false}
function Enable-ScheduledTask {$script:trace.Add('enable-task');$script:task.State='Ready';$script:task.Settings.Enabled=$true}
function Disable-ScheduledTask {$script:trace.Add('disable-task');$script:task.State='Disabled';$script:task.Settings.Enabled=$false}
function Start-Sleep {}
function Start-ScheduledTask {
 $script:trace.Add('start')
 if ($script:failure -eq 'start') {throw 'synthetic start failure'}
 $folder=Join-Path $probeRoot $script:journal.nonce;New-Item -ItemType Directory -Path $folder | Out-Null
 $nonce=if ($script:failure -eq 'bad_receipt') {'wrong'} else {$script:journal.nonce}
 Write-NewJson (Join-Path $folder 'result.json') @{passed=$true;nonce=$nonce;sid=$expectedSid;provider_requests=0;fits=0;execution_ready=$false}
}
function Get-ScheduledTaskInfo {return @{LastTaskResult=0}}
function Restore-Deployment {$script:trace.Add('restore');$script:task.State='Disabled';$script:task.Settings.Enabled=$false;$script:account.Enabled=$false}
function Export-ScheduledTask {return 'original'}
$failed=$false
try {Invoke-RetainedProbe} catch {$failed=$true} finally {Restore-Probe}
Require ($failed -eq ($script:failure -ne 'none')) 'wrong probe result'
Require (-not $script:account.Enabled -and -not $script:task.Settings.Enabled) 'identity left enabled'
Require ($script:task.Actions[0].Arguments -eq $originalArguments) 'original action not restored'
Require ($script:journal.probe_verified -eq ($script:failure -eq 'none')) 'failed probe claimed success'
Require ($script:task.Triggers -eq $null) 'probe installed triggers'
Require ($script:trace.Contains('restore')) 'rollback skipped'
'''
    run_ps(code.replace('__FAILURE__',failure),tmp_path)


@pytest.mark.parametrize('case', ['unchanged', 'restore', 'auth_failure', 'xml_drift', 'journal_failure'])
def test_task_restore_is_idempotent_and_records_incomplete_outcomes(tmp_path,case):
    code=IDENTITY+r'''
$script:case='__CASE__';$script:setCalls=0
$journalPath=Join-Path $testRoot 'journal.json'
$script:journal=[ordered]@{sid=$expectedSid;files=@{};acls=@{};original_task_xml='original';probe_verified=$false;status='prepared'}
if ($script:case -in @('restore','auth_failure','journal_failure')) {$script:task.Actions[0].Arguments=$probeArguments}
function Disable-LocalUser {$script:account.Enabled=$false}
function Disable-ScheduledTask {$script:task.State='Disabled';$script:task.Settings.Enabled=$false}
function Enable-LocalUser {throw 'UNSAFE activation'}
function Enable-ScheduledTask {throw 'UNSAFE activation'}
function Start-ScheduledTask {throw 'UNSAFE activation'}
function Restore-Deployment {$script:journal.status='rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained';Save-Journal}
function New-ScheduledTaskAction {param($Execute,$Argument,$WorkingDirectory);return [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory}}
function Set-ScheduledTask {
 param($Action)
 $script:setCalls++
 if ($script:case -ne 'restore') {
  Write-Error -Message 'synthetic task authentication failure' -Category AuthenticationError -ErrorId 'HRESULT 0x8007052e,Set-ScheduledTask' -ErrorAction Stop
 }
 $script:task.Actions=@($Action)
}
function Export-ScheduledTask {
 if ($script:case -eq 'xml_drift') {return 'unrelated XML drift'}
 if ($script:task.Actions[0].Arguments -eq $originalArguments) {return 'original'}
 return 'probe'
}
function Assert-OldTasks {}
$script:saveJournal=(Get-Item -LiteralPath Function:\Save-Journal).ScriptBlock
function Save-Journal {
 if ($script:case -eq 'journal_failure' -and $script:journal.status -eq 'rollback_needs_inspection') {throw 'synthetic journal failure'}
 & $script:saveJournal
}
$failure=$null
try {Restore-Probe} catch {$failure=$_}
$saved=Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
Require (-not $script:account.Enabled -and -not $script:task.Settings.Enabled) 'identity left enabled'
Require (-not $saved.probe_verified) 'rollback claimed a successful identity probe'
if ($script:case -in @('unchanged','restore')) {
 Require ($null -eq $failure) ('successful restore failed: '+$failure)
 Require ($saved.status -eq 'offline_probe_restored_account_and_task_disabled') 'completion not saved'
 $expectedCalls=if ($script:case -eq 'restore') {1} else {0}
 Require ($script:setCalls -eq $expectedCalls) 'unnecessary task rewrite'
 Restore-Probe
 Require ($script:setCalls -eq $expectedCalls) 'repeated restore rewrote the task'
} else {
 Require ($null -ne $failure) 'failure swallowed'
 Require ($script:journal.status -eq 'rollback_needs_inspection') 'incomplete state not retained'
 if ($script:case -eq 'xml_drift') {
  Require ($script:setCalls -eq 0) 'unchanged action rewritten to conceal XML drift'
  Require ($saved.rollback_error -like 'Restored task definition differs*') 'XML failure not recorded'
 } else {
  Require ($script:setCalls -eq 1) 'authentication retry or fallback attempted'
  Require ($failure.FullyQualifiedErrorId -like '*0x8007052e*') 'original authentication error lost'
  if ($script:case -ne 'journal_failure') {
   Require ($saved.rollback_error_id -like '*0x8007052e*') 'authentication error ID not saved'
  }
 }
 $expectedStatus=if ($script:case -eq 'journal_failure') {'offline_probe_task_restore_pending'} else {'rollback_needs_inspection'}
 Require ($saved.status -eq $expectedStatus) 'disk receipt falsely claims full restore'
}
'''
    run_ps(code.replace('__CASE__',case),tmp_path)


def test_rollback_refuses_unknown_acl_before_touching_system(tmp_path):
    run_ps(IDENTITY+r'''
$workspace=Join-Path $testRoot 'workspace';$repo=$testRoot;$live=Join-Path $testRoot 'live'
$probeAttempt=Join-Path $runtime 'attempt';$release=Join-Path $probeAttempt 'release'
$probeScripts=Join-Path $probeAttempt 'scripts';$probeRoot=Join-Path $probeAttempt 'results';$environment=Join-Path $runtime 'config\research.env'
$script:journal=@{sid=$expectedSid;files=@{};acls=@{ 'C:\Windows'='untrusted' }}
function Restore-Deployment {throw 'UNSAFE system call was reached'}
$message='';try {Restore-Probe} catch {$message=$_.Exception.Message}
Require ($message -eq 'Unexpected recovery ACL target.') 'unsafe rollback path reached system calls'
''',tmp_path)


def test_retained_probe_extracts_fixed_package_without_overwrite(tmp_path):
    code=r'''
$package='__PACKAGE__';$zipHash='294fbc9d7488be9bce86561e0df861bd9ef911a3389ec37bd9e7e29f404fbf67'
$repo='__ROOT__';$probeHash='c692467bf751d635dfb403acfd35991d5221c215909643844b4107560ff6dffe'
$manifestHash='4597017434b29ba4b388797ee7876c2d607ef7ba4a8e94679b7823a07193cde4'
$release=Join-Path $testRoot 'release'
Test-ProbePackage
Require (-not (Test-Path -LiteralPath $release)) 'check extracted files'
Test-ProbePackage $true
Require ((Get-ChildItem -LiteralPath $release -Recurse -File).Count -eq 58) 'wrong extraction count'
Reject {Test-ProbePackage $true}
'''.replace('__PACKAGE__',str(ROOT/'reports/repair-20260916/stock-data-workspace-0.3.12.zip')).replace('__ROOT__',str(ROOT))
    # Reports are not versioned: use a bounded synthetic ZIP in CI, not a
    # dependency on a workstation's private historical artifact.
    import zipfile
    import hashlib
    archive=tmp_path/'test-release.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('research-release.json','{}')
        for i in range(57):z.writestr(f'files/{i}.txt',str(i))
    code=code.replace(str(ROOT/'reports/repair-20260916/stock-data-workspace-0.3.12.zip'),str(archive))
    code=code.replace('294fbc9d7488be9bce86561e0df861bd9ef911a3389ec37bd9e7e29f404fbf67',hashlib.sha256(archive.read_bytes()).hexdigest())
    code=code.replace('4597017434b29ba4b388797ee7876c2d607ef7ba4a8e94679b7823a07193cde4',hashlib.sha256(b'{}').hexdigest())
    # The historical Apply hash stays fixed; its test uses a synthetic probe,
    # not the current deployment tool, which has replaced the old contract.
    fixture_root=tmp_path/'repo';(fixture_root/'tools/v2').mkdir(parents=True)
    (fixture_root/'tools/v2/deployment_probe.py').write_text('# synthetic probe',encoding='utf-8')
    code=code.replace(str(ROOT),str(fixture_root)).replace(
        'c692467bf751d635dfb403acfd35991d5221c215909643844b4107560ff6dffe',
        hashlib.sha256(b'# synthetic probe').hexdigest())
    run_ps(code,tmp_path)


def test_recovery_inspection_does_not_certify_or_unlock_replaced_probe():
    text=SCRIPT.read_text(encoding='utf-8')
    check=text.split("if ($requestedMode -eq 'Check') {",1)[1].split('\ntry {',1)[0]
    assert 'Assert-RetainedRollback' in check
    assert 'Test-ProbePackage' not in check
    assert '$report.probe_package_checked=$false' in check
    assert 'Assert-RetainedRollback;Test-ProbePackage;Assert-Idle' in text


def test_window_cannot_be_extended_or_used_after_source_change(tmp_path):
    code=r'''
$probeAttempt=$testRoot;$permitPath=Join-Path $testRoot 'permit.json';$originalJournal=Join-Path $testRoot 'original.json'
$helper='__HELPER__';$zipHash='bound-package'
function Private-Directory {}
function Get-FileHash {param($LiteralPath);return @{Hash=if ($LiteralPath -eq $originalJournal) {'journal'} else {'source'}}}
$permit=@{scope='retained_identity_0312_offline_probe_only';started_utc=[DateTimeOffset]::UtcNow.AddMinutes(-1).ToString('o');expires_utc=[DateTimeOffset]::UtcNow.AddMinutes(59).ToString('o');installer_sha256='source';helper_sha256='source';package_sha256=$zipHash;original_journal_sha256='journal'}
$start=[DateTimeOffset]::Parse($permit.started_utc);$permit.expires_utc=$start.AddHours(1).ToString('o')
function Get-Content {return ($script:permit | ConvertTo-Json)}
Assert-ProbeWindow
$script:permit.expires_utc=$start.AddHours(2).ToString('o');Reject {Assert-ProbeWindow}
$script:permit.started_utc=$start.AddHours(-2).ToString('o');$script:permit.expires_utc=$start.AddHours(-1).ToString('o');Reject {Assert-ProbeWindow}
$script:permit.started_utc=$start.ToString('o');$script:permit.expires_utc=$start.AddHours(1).ToString('o')
foreach ($key in @('installer_sha256','helper_sha256','package_sha256','original_journal_sha256')) {
 $before=$script:permit[$key];$script:permit[$key]='changed';Reject {Assert-ProbeWindow};$script:permit[$key]=$before
}
'''.replace('__HELPER__',str(HELPER))
    run_ps(code,tmp_path)


def test_exclusive_receipt_does_not_overwrite(tmp_path):
    run_ps(r'''
$path=Join-Path $testRoot 'receipt.json'
Write-NewJson $path @{original=$true}
Reject {Write-NewJson $path @{original=$false}}
''',tmp_path)
    assert json.loads((tmp_path/'receipt.json').read_text())=={'original':True}


def test_rollback_acl_comparison_does_not_reinstate_transient_inherited_rights(tmp_path):
    run_ps(r'''
$base='D:AI(A;;FA;;;BA)(A;;FA;;;SY)'
$transient='D:AI(A;;FA;;;BA)(A;;FA;;;SY)(A;ID;0x1301bf;;;S-1-5-21-1-2-3-1006)'
Require ((Explicit-Dacl $base) -ceq (Explicit-Dacl $transient)) 'transient inherited ACE treated as prior authority'
Require ((Explicit-Dacl $base) -cne (Explicit-Dacl 'D:PAI(A;;FA;;;BA)(A;;FA;;;SY)')) 'protection change missed'
Require ((Explicit-Dacl $base) -cne (Explicit-Dacl 'D:AI(A;;FA;;;BA)(A;;FR;;;SY)')) 'explicit access change missed'
''',tmp_path)


def test_private_attempt_allows_only_noninherited_read_traversal(tmp_path):
    run_ps(r'''
$expectedSid='S-1-5-21-1-2-3-1006'
$script:acl=[Security.AccessControl.DirectorySecurity]::new()
$script:acl.SetAccessRuleProtection($true,$false)
foreach ($id in @('S-1-5-18','S-1-5-32-544')) {
 $sid=[Security.Principal.SecurityIdentifier]::new($id)
 $script:acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
function Get-Acl {return $script:acl}
Private-Directory $testRoot
$research=[Security.Principal.SecurityIdentifier]::new($expectedSid)
$rule=[Security.AccessControl.FileSystemAccessRule]::new($research,'ReadAndExecute','None','None','Allow')
$script:acl.AddAccessRule($rule)
Reject {Private-Directory $testRoot}
Private-Directory $testRoot $true
$script:acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($research,'Modify','None','None','Allow'))
Reject {Private-Directory $testRoot $true}
''',tmp_path)


def test_known_retained_state_rejects_journal_and_policy_drift(tmp_path):
    run_ps(IDENTITY+r'''
$repo=$testRoot;$live=$testRoot;$workspace=$testRoot;$python='python.exe'
$originalState=$testRoot;$originalJournal='original';$bundle=$testRoot;$environment='market-environment';$hotfixHash='synthetic'
$saved=[pscustomobject]@{status='rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained';user_created=$true;sid=$expectedSid;installer_sha256='7114316bde2548f78054570269fee4e0a31a34480b78bd36b7ec207ee16e4de4';acls=[pscustomobject]@{};files=[pscustomobject]@{};old_tasks=[pscustomobject]@{synthetic='xml'}}
foreach ($name in @('a','b','c')) {$saved.files | Add-Member -MemberType NoteProperty -Name $name -Value @{before_sha256='before';after_sha256='after'}}
$manifest=[pscustomobject]@{files=$saved.files}
$script:rights=@{DirectLocalGroups=@(@{Sid='S-1-5-32-545'});BatchPolicies=@('SeBatchLogonRight = *'+$expectedSid)}
function Assert-PlainPath {}
function Private-Directory {}
function Assert-Hash {}
function Get-Content {param($LiteralPath);if ($LiteralPath -eq $originalJournal) {return ($script:saved | ConvertTo-Json -Depth 5)};return ($script:manifest | ConvertTo-Json -Depth 5)}
function Old-Tasks {return [ordered]@{synthetic='xml'}}
function Get-BatchRightsDiagnostic {return $script:rights}
function Assert-BatchLogon {}
function Get-Item {return @{Length=0}}
# Reach the policy guard before any environment-file read.
$saved.user_created=$false;Reject {Assert-RetainedRollback};$saved.user_created=$true
$saved.sid='different';Reject {Assert-RetainedRollback};$saved.sid=$expectedSid
$saved.installer_sha256='different';Reject {Assert-RetainedRollback};$saved.installer_sha256='7114316bde2548f78054570269fee4e0a31a34480b78bd36b7ec207ee16e4de4'
$script:rights.DirectLocalGroups=@(@{Sid='S-1-5-32-544'})
$message='';try {Assert-RetainedRollback} catch {$message=$_.Exception.Message}
Require ($message -eq 'Unexpected dedicated account group membership.') ('group guard not reached: '+$message)
$script:rights.DirectLocalGroups=@(@{Sid='S-1-5-32-545'});$script:rights.BatchPolicies=@('SeDenyBatchLogonRight = *unknown')
$message='';try {Assert-RetainedRollback} catch {$message=$_.Exception.Message}
Require ($message -like 'Batch deny policy differs*') 'deny drift not rejected'
''',tmp_path)


@pytest.mark.parametrize('case',['cancel','success','probe_failure','rollback_failure'])
def test_real_entrypoint_control_flow_is_one_use_and_failure_closed(tmp_path,case):
    # Execute the real dispatch and try/finally flow in a NEW synthetic script.
    # Replace every OS operation; only permit/journal files in tmp_path are real.
    source=SCRIPT.read_text().replace("$helper=Join-Path $PSScriptRoot 'deploy_research_cutover.ps1'",f"$helper='{HELPER}'")
    marker='\nAssert-Admin\nforeach ($path'
    assert source.count(marker)==1
    (tmp_path/'original.json').write_text('{}')
    override=r'''
$testRoot='__TEMP__';$probeAttempt=Join-Path $testRoot 'attempt'
$probeRoot=Join-Path $probeAttempt 'results'
$stateDir=$probeAttempt;$journalPath=Join-Path $probeAttempt 'journal.json';$permitPath=Join-Path $probeAttempt 'permit.json'
$originalJournal=Join-Path $testRoot 'original.json'
function Assert-Admin {}
function Assert-PlainPath {}
function Assert-RetainedRollback {}
function Test-ProbePackage {}
function Assert-Idle {}
function Assert-MaintenanceHostTimezone {}
function Assert-ScheduleClear {}
function Hold-ProbeGuards {}
function Read-Host {return '__CONFIRMATION__'}
function Lock-NewDirectory {param($Path);New-Item -ItemType Directory -Path $Path | Out-Null}
function Export-ScheduledTask {return 'unchanged-task'}
function Old-Tasks {return [ordered]@{original='unchanged'}}
function Get-ProbeDiagnostic {return @{system_changes=0;synthetic=$true}}
function Invoke-RetainedProbe {
 if ('__CASE__' -eq 'probe_failure') {throw 'synthetic probe failure'}
 $script:journal.probe_verified=$true;Save-Journal
}
function Restore-Probe {
 if ('__CASE__' -eq 'rollback_failure') {throw 'synthetic rollback failure'}
 $script:journal.status='synthetic_restored';Save-Journal
}
'''.replace('__TEMP__',str(tmp_path)).replace('__CASE__',case).replace('__CONFIRMATION__','start' if case=='cancel' else 'START')
    isolated=tmp_path/'isolated.ps1'
    isolated.write_text(source.replace(marker,'\n'+override+marker),encoding='utf-8')
    command=['powershell.exe','-NoProfile','-NonInteractive','-File',str(isolated),'-Mode','Apply']
    env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'}
    result=subprocess.run(command,capture_output=True,text=True,timeout=30,env=env)
    attempt=tmp_path/'attempt'
    if case=='cancel':
        assert result.returncode!=0 and not attempt.exists()
        assert 'Cancelled' in result.stderr
        return
    journal=json.loads((attempt/'journal.json').read_text())
    if case=='success':
        assert result.returncode==0,result.stdout+result.stderr
        assert 'OFFLINE_PROBE_COMPLETE' in result.stdout
        assert journal['probe_verified'] and journal['status']=='synthetic_restored'
    else:
        assert result.returncode!=0 and 'OFFLINE_PROBE_COMPLETE' not in result.stdout
        if case=='probe_failure':
            assert not journal['probe_verified'] and journal['failure_reason']=='synthetic probe failure'
            assert journal['status']=='synthetic_restored'
            assert journal['failure_diagnostic']['LaunchLogPath']==str(attempt/'results/launch.log')
            assert journal['failure_diagnostic']['LaunchLogPresent'] is False
        else:
            assert journal['status']=='rollback_needs_inspection'
    before={p.name:p.read_bytes() for p in attempt.iterdir()}
    repeated=subprocess.run(command,capture_output=True,text=True,timeout=30,env=env)
    assert repeated.returncode!=0
    assert 'already exists' in repeated.stderr
    assert before=={p.name:p.read_bytes() for p in attempt.iterdir()}


def test_independent_acl_change_disables_identity_but_is_not_overwritten(tmp_path):
    run_ps(IDENTITY+r'''
$repo=$testRoot;$workspace=$testRoot;$live=$testRoot
$probeAttempt=Join-Path $runtime 'attempt';$release=Join-Path $probeAttempt 'release'
$probeScripts=Join-Path $probeAttempt 'scripts';$probeRoot=Join-Path $probeAttempt 'results';$environment=Join-Path $runtime 'config\research.env'
$before=[Security.AccessControl.DirectorySecurity]::new()
$before.SetAccessRuleProtection($true,$false)
$before.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'),'FullControl','None','None','Allow'))
$script:journal=@{sid=$expectedSid;files=@{};acls=@{}}
$script:journal.acls[$testRoot]=$before.Sddl
$before.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'),'Read','None','None','Allow'))
function Get-Acl {return $script:before}
function Disable-LocalUser {$script:account.Enabled=$false}
function Disable-ScheduledTask {$script:task.Settings.Enabled=$false}
function Restore-Deployment {throw 'UNSAFE overwrite reached'}
$script:account.Enabled=$true;$script:task.Settings.Enabled=$true;$script:task.State='Ready'
$message='';try {Restore-Probe} catch {$message=$_.Exception.Message}
Require ($message -like 'ACL changed independently*') 'independent ACL change not protected'
Require (-not $script:account.Enabled -and -not $script:task.Settings.Enabled) 'identity remained enabled after failed rollback'
''',tmp_path)


def test_journal_replacement_retains_interrupted_write_evidence(tmp_path):
    run_ps(r'''
$journalPath=Join-Path $testRoot 'journal.json'
$script:journal=@{step=1};Save-Journal
$script:journal=@{step=2};Save-Journal
Require ((Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json).step -eq 2) 'atomic replacement failed'
Write-NewJson ($journalPath+'.new') @{step=3}
$script:journal=@{step=4};Reject {Save-Journal}
Require ((Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json).step -eq 2) 'authoritative journal overwritten'
Require ((Get-Content -LiteralPath ($journalPath+'.new') -Raw | ConvertFrom-Json).step -eq 3) 'interrupted evidence overwritten'
''',tmp_path)
