[CmdletBinding()]
param([ValidateSet('Check','Apply','Rollback')][string]$Mode='Check')

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$requestedMode=$Mode
$helper=Join-Path $PSScriptRoot 'deploy_research_cutover.ps1'
$helperHash='a140248b353f22a92d934d27a22efb675ee0cf8b48a8a8f3383634fd69889027'
if ((Get-FileHash -LiteralPath $helper -Algorithm SHA256).Hash -ne $helperHash) { throw 'Deployment helper changed; no recovery action permitted.' }
. $helper -Mode LoadLibrary

# This is a reversible OFFLINE probe of the retained identity, not a cutover.
# No account/password recreation, trigger registration, legacy file replacement,
# network acquisition, or reuse of the expired 0.3.10 maintenance permit.
$originalJournal=Join-Path $runtime 'deployment-20260913\journal.json'
$originalState=Join-Path $runtime 'deployment-20260913'
$probeAttempt=Join-Path $runtime 'recovery-probe-0312-once'
$stateDir=$probeAttempt
$journalPath=Join-Path $probeAttempt 'journal.json'
$permitPath=Join-Path $probeAttempt 'permit.json'
$release=Join-Path $probeAttempt 'release'
$probeScripts=Join-Path $probeAttempt 'scripts'
$probeRoot=Join-Path $probeAttempt 'results'
$environment=Join-Path $runtime 'config\research.env'
$package=Join-Path $repo 'reports\repair-20260916\stock-data-workspace-0.3.12.zip'
$zipHash='294fbc9d7488be9bce86561e0df861bd9ef911a3389ec37bd9e7e29f404fbf67'
$manifestHash='4597017434b29ba4b388797ee7876c2d607ef7ba4a8e94679b7823a07193cde4'
$expectedSid='S-1-5-21-3027070730-734606845-1610825463-1006'
$script:researchSid=[Security.Principal.SecurityIdentifier]::new($expectedSid)
$probeLauncher=Join-Path $probeScripts 'identity-probe.ps1'
$expectedExecute=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$originalArguments='-NoProfile -NonInteractive -File "'+(Join-Path $runtime 'scripts\identity-probe.ps1')+'"'
$probeArguments='-NoProfile -NonInteractive -File "'+$probeLauncher+'"'

function Private-Directory([string]$Path,[bool]$AllowProbeTraversal=$false) {
    Assert-PlainPath $Path
    $acl=Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) { throw 'Expected private recovery directory.' }
    $allowed=@()
    foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq 'Allow') {
            if ($AllowProbeTraversal -and $rule.IdentityReference.Value -eq $expectedSid -and
                $rule.InheritanceFlags -eq [Security.AccessControl.InheritanceFlags]::None -and
                ([long]$rule.FileSystemRights -band (-bnot [long][Security.AccessControl.FileSystemRights]'ReadAndExecute,Synchronize')) -eq 0) { continue }
            if ($rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544')) { throw 'Unexpected private recovery directory access.' }
            if (($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl) { $allowed+=$rule.IdentityReference.Value }
        }
    }
    if ('S-1-5-18' -notin $allowed -or 'S-1-5-32-544' -notin $allowed) { throw 'Incomplete administrative access.' }
}
function Assert-ProbeIdentity([bool]$RequireDisabled=$true) {
    $account=Get-LocalUser -Name $userName -ErrorAction Stop
    $task=Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
    if ($account.SID.Value -ne $expectedSid -or
        $task.Principal.UserId -notin @($expectedSid,$userName,"$env:COMPUTERNAME\$userName") -or
        [string]$task.Principal.LogonType -ne 'Password' -or [string]$task.Principal.RunLevel -ne 'Limited') { throw 'Retained identity/principal mismatch; no reset or fallback allowed.' }
    if (@($task.Triggers | Where-Object {$null -ne $_}).Count -ne 0 -or @($task.Actions | Where-Object {$null -ne $_}).Count -ne 1) { throw 'Expected exactly one offline action and no triggers.' }
    if ($RequireDisabled -and ($account.Enabled -or $task.State -ne 'Disabled' -or $task.Settings.Enabled)) { throw 'Retained account and task must both be disabled.' }
    $action=$task.Actions[0]
    if ($action.Execute -ne $expectedExecute -or $action.WorkingDirectory -ne $runtime -or
        $action.Arguments -notin @($originalArguments,$probeArguments)) { throw 'Unrelated task action; recovery refused.' }
    return $task
}
function Assert-RestorePath([string]$Path) {
    $full=[IO.Path]::GetFullPath($Path)
    $exact=@($runtime,$probeAttempt,$release,$probeScripts,$probeRoot,$environment,'D:\anaconda',
        (Join-Path $runtime 'config'),(Join-Path $live 'kpl_data.duckdb'),
        (Join-Path $repo 'data\research-inputs'),$workspace)
    $children=@('builds','price-studies','retraining','notes','judgement.guard','research-current.json',
        'research-candidate.json','price-study-current.json','workspace-config.json')
    $exact+=@($children | ForEach-Object {Join-Path $workspace $_})
    if ($full -notin $exact) { throw 'Unexpected recovery ACL target.' }
    Assert-PlainPath $full
}
function Explicit-Dacl([string]$Sddl) {
    $descriptor=[Security.AccessControl.RawSecurityDescriptor]::new($Sddl)
    $protected=($descriptor.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected) -ne 0
    $entries=@()
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if (([int]$ace.AceFlags -band [int][Security.AccessControl.AceFlags]::Inherited) -eq 0) {
            $bytes=[byte[]]::new($ace.BinaryLength);$ace.GetBinaryForm($bytes,0)
            $entries+=[BitConverter]::ToString($bytes)
        }
    }
    return ([string]$protected+'|'+($entries -join '|'))
}
function Assert-RetainedRollback {
    foreach ($path in @($repo,$live,$runtime,$originalJournal,$workspace,$environment,$python)) { Assert-PlainPath $path }
    Private-Directory $runtime; Private-Directory $originalState
    $saved=Get-Content -LiteralPath $originalJournal -Raw | ConvertFrom-Json
    if ($saved.status -ne 'rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained' -or
        $saved.user_created -isnot [bool] -or -not $saved.user_created -or $saved.sid -ne $expectedSid -or
        $saved.installer_sha256 -ne '7114316bde2548f78054570269fee4e0a31a34480b78bd36b7ec207ee16e4de4') { throw 'Not the retained second-failure rollback; manual inspection required.' }
    $task=Assert-ProbeIdentity
    if ($task.Actions[0].Arguments -ne $originalArguments) { throw 'Task action is not the original disabled probe.' }
    Assert-Hash (Join-Path $bundle 'manifest.json') $hotfixHash
    $manifest=Get-Content -LiteralPath (Join-Path $bundle 'manifest.json') -Raw | ConvertFrom-Json
    if (@($saved.files.PSObject.Properties).Count -ne 3) { throw 'Unexpected original backup set.' }
    foreach ($p in $saved.files.PSObject.Properties) {
        $expected=$manifest.files.PSObject.Properties[$p.Name]
        if (-not $expected -or $p.Value.before_sha256 -ne $expected.Value.before_sha256 -or $p.Value.after_sha256 -ne $expected.Value.after_sha256) { throw 'Original backup manifest mismatch.' }
        Assert-Hash (Join-Path $live $p.Name) $p.Value.before_sha256
        Assert-Hash (Join-Path (Join-Path $originalState 'before') $p.Name) $p.Value.before_sha256
    }
    # The original journal is private. Check every original ACL was restored;
    # old release/script paths are allowed only below this exact retained root.
    foreach ($p in $saved.acls.PSObject.Properties) {
        $full=[IO.Path]::GetFullPath($p.Name)
        if (-not $full.StartsWith($runtime+'\',[StringComparison]::OrdinalIgnoreCase)) { Assert-RestorePath $full }
        Assert-PlainPath $full
        # The original writer snapshotted children AFTER granting their parent.
        # Its saved inherited research ACEs are transient, not the pre-deploy
        # authority. Compare explicit rules/protection and reject any retained
        # research ACE, inherited or explicit, after the parent was restored.
        $actualAcl=Get-Acl -LiteralPath $full
        if ((Explicit-Dacl $actualAcl.Sddl) -cne (Explicit-Dacl ([string]$p.Value)) -or
            @($actualAcl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object {$_.IdentityReference.Value -eq $expectedSid}).Count) { throw 'Original ACL not restored; no recovery apply allowed.' }
    }
    $current=Old-Tasks
    if (@($saved.old_tasks.PSObject.Properties).Count -ne $current.Count) { throw 'Original task inventory changed.' }
    foreach ($p in $saved.old_tasks.PSObject.Properties) {
        if (-not $current.Contains($p.Name) -or $current[$p.Name] -cne $p.Value) { throw 'Original task changed.' }
    }
    if ((Get-ScheduledTask -TaskName 'StockData-MonthlyCompact').State -ne 'Disabled') { throw 'Monthly compact must remain disabled.' }
    $rights=Get-BatchRightsDiagnostic
    if (@($rights.DirectLocalGroups | Where-Object {$_.Sid -ne 'S-1-5-32-545'}).Count) { throw 'Unexpected dedicated account group membership.' }
    if (@($rights.BatchPolicies | Where-Object {$_ -match '^SeDenyBatchLogonRight\s*=\s*\S'}).Count) { throw 'Batch deny policy differs from the reviewed empty policy; manual review required.' }
    Assert-BatchLogon
    # Never print market credentials or copy them into the recovery report.
    if ((Get-Item -LiteralPath $environment).Length -gt 16384) { throw 'Unexpected environment file size.' }
    $keys=@()
    foreach ($line in [IO.File]::ReadAllLines($environment)) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([A-Z_]+)=(.+)$' -or $Matches[1] -notin @('HITHINK_FINANCE_API_KEY','XIAODEFA_TOKEN','TUSHARE_XIAODEFA_TOKEN','XIAODEFA_URL')) { throw 'Unexpected environment entry; do not expose its content.' }
        $keys+=$Matches[1]
    }
    if ('HITHINK_FINANCE_API_KEY' -notin $keys -or ('XIAODEFA_TOKEN' -notin $keys -and 'TUSHARE_XIAODEFA_TOKEN' -notin $keys)) { throw 'Market-only environment incomplete.' }
}
function Test-ProbePackage([bool]$Extract=$false) {
    Assert-PlainPath $package; Assert-Hash $package $zipHash
    Assert-Hash (Join-Path $repo 'tools\v2\deployment_probe.py') $probeHash
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($package)
    try {
        $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $total=0L
        if ($zip.Entries.Count -ne 58) { throw 'Unexpected release member count.' }
        foreach ($entry in $zip.Entries) {
            $name=$entry.FullName
            $target=[IO.Path]::GetFullPath((Join-Path $release $name))
            if ($name -match '[:\\]' -or -not $seen.Add($name) -or
                -not $target.StartsWith($release+'\',[StringComparison]::OrdinalIgnoreCase) -or $entry.Length -gt 100MB) { throw 'Unsafe or duplicate release member.' }
            $total+=$entry.Length
            if ($total -gt 500MB) { throw 'Release size budget exceeded.' }
        }
        if ($Extract) {
            if (Test-Path -LiteralPath $release) { throw 'Never overwrite a prepared release.' }
            New-Item -ItemType Directory -Path $release | Out-Null
            foreach ($entry in $zip.Entries) {
                $target=Join-Path $release $entry.FullName
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$target,$false)
            }
            Assert-Hash (Join-Path $release 'research-release.json') $manifestHash
        }
    } finally {$zip.Dispose()}
}
function Write-NewJson([string]$Path,$Value) {
    $stream=[IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 12));$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
}
function Assert-ProbeWindow {
    Private-Directory $probeAttempt $true
    $permit=Get-Content -LiteralPath $permitPath -Raw | ConvertFrom-Json
    $start=[DateTimeOffset]::Parse($permit.started_utc);$end=[DateTimeOffset]::Parse($permit.expires_utc);$now=[DateTimeOffset]::UtcNow
    if ($permit.scope -ne 'retained_identity_0312_offline_probe_only' -or ($end-$start).TotalSeconds -ne 3600 -or $now -lt $start -or $now -ge $end -or
        $permit.installer_sha256 -ne (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash -or
        $permit.helper_sha256 -ne (Get-FileHash -LiteralPath $helper -Algorithm SHA256).Hash -or $permit.package_sha256 -ne $zipHash -or
        $permit.original_journal_sha256 -ne (Get-FileHash -LiteralPath $originalJournal -Algorithm SHA256).Hash) { throw 'Offline probe authorization absent, expired or changed; never renew it.' }
}
function Hold-ProbeGuards {
    foreach ($path in @((Join-Path $live 'kpl_data.duckdb.pipeline.lock.guard'),(Join-Path $workspace 'update.guard'),(Join-Path $workspace 'publication.guard'),(Join-Path $workspace 'judgement.guard'))) { Hold-Guard $path }
}
function Restore-Probe {
    $task=Assert-ProbeIdentity $false
    foreach ($path in $script:journal.acls.Keys) {Assert-RestorePath $path}
    if ($script:journal.files.Count -ne 0 -or $script:journal.sid -ne $expectedSid) { throw 'Unexpected recovery restore journal.' }
    # Fail closed BEFORE inspecting ACL drift. An unrelated permission change
    # must not leave the known temporary identity able to start again.
    Disable-LocalUser -Name $userName
    Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null
    if ($task.State -eq 'Running') {throw 'Probe still running; future starts disabled. Do not kill it; run Rollback after it exits.'}
    foreach ($path in $script:journal.acls.Keys) {
        $candidate=Get-Acl -LiteralPath $path
        foreach ($rule in $candidate.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier])) {
            if ($rule.IdentityReference.Value -eq $expectedSid) {$candidate.RemoveAccessRuleSpecific($rule)}
        }
        if ((Explicit-Dacl $candidate.Sddl) -cne (Explicit-Dacl $script:journal.acls[$path])) {throw 'ACL changed independently; do not overwrite unrelated permission changes.'}
    }
    Restore-Deployment
    $script:journal.status='offline_probe_task_restore_pending';Save-Journal
    try {
        # The validator fixes the identity, executable, working directory and
        # trigger count. Do not re-register an already-original password action.
        $task=Assert-ProbeIdentity
        if ($task.Actions[0].Arguments -ne $originalArguments) {
            $action=New-ScheduledTaskAction -Execute $expectedExecute -Argument $originalArguments -WorkingDirectory $runtime
            Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action | Out-Null
        }
        $null=Assert-ProbeIdentity
        if ((Export-ScheduledTask -TaskName $taskName -TaskPath '\') -cne $script:journal.original_task_xml) { throw 'Restored task definition differs; retain disabled state and inspect.' }
        Assert-OldTasks
    } catch {
        $restoreError=$_
        $script:journal.status='rollback_needs_inspection'
        $script:journal['rollback_error']=$restoreError.Exception.Message
        $script:journal['rollback_error_id']=$restoreError.FullyQualifiedErrorId
        try {Save-Journal} catch {Write-Warning 'Unable to save rollback failure; retain pending journal and all evidence.'}
        throw $restoreError
    }
    $script:journal.status='offline_probe_restored_account_and_task_disabled';Save-Journal
}
function Invoke-RetainedProbe {
    Assert-ProbeWindow; Test-ProbePackage $true
    New-Item -ItemType Directory -Path $probeScripts,$probeRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $repo 'tools\v2\deployment_probe.py') -Destination $probeScripts
    $nonce=$script:journal.nonce;$probeOutput=Join-Path $probeRoot $nonce
    $arguments='-I -X utf8 -B "'+(Join-Path $probeScripts 'deployment_probe.py')+'" --release "'+$release+'" --workspace "'+$workspace+'" --database "'+(Join-Path $live 'kpl_data.duckdb')+'" --output "'+$probeOutput+'" --nonce '+$nonce+' --sid '+$expectedSid+' --environment "'+$environment+'"'
    $body="`$ErrorActionPreference='Stop'`r`n`$env:KPL_ENV_FILE='"+$environment+"'`r`n& '"+$python+"' "+$arguments+" *> '"+(Join-Path $probeRoot 'launch.log')+"'`r`nexit `$LASTEXITCODE`r`n"
    [IO.File]::WriteAllText($probeLauncher,$body,[Text.UTF8Encoding]::new($false))
    Grant-Access $runtime 'ReadAndExecute'
    Grant-Access $probeAttempt 'ReadAndExecute'
    Grant-Access $release 'ReadAndExecute' $true
    Grant-Access $probeScripts 'ReadAndExecute' $true
    Grant-Access (Join-Path $runtime 'config') 'ReadAndExecute'
    Grant-Access $environment 'Read'
    Grant-Access 'D:\anaconda' 'ReadAndExecute' $true
    Grant-Access (Join-Path $live 'kpl_data.duckdb') 'Read'
    Grant-Access (Join-Path $live 'kpl_data.duckdb') 'Write,Delete,ChangePermissions,TakeOwnership' $false 'Deny'
    Grant-Access (Join-Path $repo 'data\research-inputs') 'ReadAndExecute' $true
    Grant-Access $workspace 'Modify' $true
    foreach ($name in @('builds','price-studies','retraining','notes','judgement.guard','research-current.json','research-candidate.json','price-study-current.json','workspace-config.json')) {
        $path=Join-Path $workspace $name
        if (Test-Path -LiteralPath $path) { Grant-Access $path 'Write,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership' (Test-Path -LiteralPath $path -PathType Container) 'Deny' }
    }
    Grant-Access $probeRoot 'Modify' $true
    # The private journal and permit never inherit the research identity's RX.
    Assert-ProbeWindow; Assert-BatchLogon
    $null=Assert-ProbeIdentity
    $action=New-ScheduledTaskAction -Execute $expectedExecute -Argument $probeArguments -WorkingDirectory $runtime
    Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action | Out-Null
    $script:journal.status='offline_probe_starting';Save-Journal
    Enable-LocalUser -Name $userName
    Enable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null
    $null=Assert-ProbeIdentity $false
    Start-ScheduledTask -TaskName $taskName -TaskPath '\'
    $deadline=[DateTimeOffset]::UtcNow.AddMinutes(4)
    do {
        Start-Sleep -Seconds 2
        $task=Get-ScheduledTask -TaskName $taskName -TaskPath '\'
        $exists=Test-Path -LiteralPath (Join-Path $probeOutput 'result.json')
    } while ((-not $exists -or $task.State -eq 'Running') -and [DateTimeOffset]::UtcNow -lt $deadline)
    if (-not $exists -or $task.State -eq 'Running' -or (Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\').LastTaskResult -ne 0) { throw 'Offline identity probe failed or timed out; no daily trigger installed.' }
    $result=Get-Content -LiteralPath (Join-Path $probeOutput 'result.json') -Raw | ConvertFrom-Json
    if ($result.passed -isnot [bool] -or -not $result.passed -or $result.nonce -ne $nonce -or $result.sid -ne $expectedSid -or $result.provider_requests -ne 0 -or $result.fits -ne 0 -or $result.execution_ready) { throw 'Invalid offline probe receipt.' }
    Assert-ProbeWindow; Assert-OldTasks
    $script:journal.probe_verified=$true; $script:journal['probe_output']=$probeOutput;Save-Journal
}

Assert-Admin
foreach ($path in @($probeAttempt,$originalJournal,$package,$helper)) { Assert-PlainPath $path }
if ($requestedMode -eq 'Check') {
    $report=[ordered]@{captured_at=[DateTimeOffset]::UtcNow.ToString('o');system_changes=0;mode='retained_offline_probe_check';passed=$false;execution_ready=$false;production_cutover=$false}
    try {
        # Recovery state is independent of the now-replaced deployment probe.
        # Apply still pins the historical package/probe and cannot be reused.
        Assert-RetainedRollback
        $report.passed=$true;$report.account_enabled=$false;$report.task_enabled=$false
        $report.attempt_already_exists=Test-Path -LiteralPath $probeAttempt
        $report.historical_package_sha256_reference=$zipHash
        $report.probe_package_checked=$false
    } catch {$report.error=$_.Exception.Message}
    $output=Join-Path $repo ('reports\repair-20260916\retained-check-'+[Guid]::NewGuid().ToString('N')+'.json')
    Write-NewJson $output $report
    Write-Output "RECOVERY_CHECK_SAVED: $output ; system changes=0."
    if (-not $report.passed) {exit 1};exit 0
}
try {
    if ($requestedMode -eq 'Rollback') {
        Private-Directory $probeAttempt $true
        $loaded=Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
        if ($loaded.scope -ne 'retained_identity_0312_offline_probe_only' -or $loaded.sid -ne $expectedSid) {throw 'Unrelated recovery journal.'}
        $script:journal=[ordered]@{scope=$loaded.scope;status=$loaded.status;sid=$loaded.sid;user_created=$true;original_task_xml=$loaded.original_task_xml;nonce=$loaded.nonce;probe_verified=$loaded.probe_verified;acls=[ordered]@{};files=[ordered]@{};old_tasks=[ordered]@{}}
        foreach ($key in @('acls','old_tasks')) {foreach ($p in $loaded.$key.PSObject.Properties) {$script:journal[$key][$p.Name]=$p.Value}}
        foreach ($key in @('probe_output','failure_diagnostic','failure_reason','rollback_error','rollback_error_id')) {if ($loaded.PSObject.Properties[$key]) {$script:journal[$key]=$loaded.$key}}
        Hold-ProbeGuards;Restore-Probe
        Write-Output 'RECOVERY_ROLLBACK_COMPLETE: account/task disabled, temporary ACLs/action restored; all evidence retained.';exit 0
    }
    Assert-RetainedRollback;Test-ProbePackage;Assert-Idle;Assert-MaintenanceHostTimezone
    if (Test-Path -LiteralPath $probeAttempt) {throw 'This recovery attempt already exists; inspect or Rollback. Never delete it or rerun Apply.'}
    $now=[DateTimeOffset]::UtcNow;Assert-ScheduleClear $now ($now.AddHours(1))
    Write-Output 'Scope: fixed 0.3.12 OFFLINE identity probe only; temporarily enable retained account/task without triggers; restore disabled state and temporary ACLs/action afterward. No daily deployment, credential reset, trading or legacy file changes.'
    $confirmation=Read-Host 'Only after this exact scope is approved, type START for one non-renewable 60-minute window; anything else cancels'
    if ($confirmation -cne 'START') {throw 'Cancelled. No recovery attempt or system changes.'}
    $start=[DateTimeOffset]::UtcNow
    Hold-ProbeGuards;Assert-RetainedRollback;Assert-Idle;Test-ProbePackage;Assert-ScheduleClear $start ($start.AddHours(1))
    if (Test-Path -LiteralPath $probeAttempt) {throw 'Another invocation already consumed the attempt.'}
    Lock-NewDirectory $probeAttempt
    Write-NewJson $permitPath ([ordered]@{scope='retained_identity_0312_offline_probe_only';started_utc=$start.ToString('o');expires_utc=$start.AddHours(1).ToString('o');sid=$expectedSid;installer_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash;helper_sha256=$helperHash;package_sha256=$zipHash;original_journal_sha256=(Get-FileHash -LiteralPath $originalJournal -Algorithm SHA256).Hash})
    $script:journal=[ordered]@{scope='retained_identity_0312_offline_probe_only';status='prepared';sid=$expectedSid;user_created=$true;original_task_xml=(Export-ScheduledTask -TaskName $taskName -TaskPath '\');nonce=[Guid]::NewGuid().ToString('N');probe_verified=$false;acls=[ordered]@{};files=[ordered]@{};old_tasks=(Old-Tasks)}
    Save-Journal
    try {Invoke-RetainedProbe} catch {
        $script:journal['failure_reason']=$_.Exception.Message
        try {
            $script:journal['failure_diagnostic']=Get-ProbeDiagnostic
            $script:journal.failure_diagnostic['LaunchLogPath']=Join-Path $probeRoot 'launch.log'
            $script:journal.failure_diagnostic['LaunchLogPresent']=Test-Path -LiteralPath (Join-Path $probeRoot 'launch.log')
            Save-Journal
        } catch {Write-Warning 'Unable to persist diagnostic; retain all evidence.'}
        throw
    } finally {
        try {Restore-Probe} catch {
            $script:journal.status='rollback_needs_inspection'
            $script:journal['rollback_error']=$_.Exception.Message
            Save-Journal
            throw
        }
    }
    Write-Output "OFFLINE_PROBE_COMPLETE: dedicated identity verified and restored disabled. No production cutover. Receipt: $journalPath"
} finally {foreach ($g in $guards) {$g.Dispose()}}
