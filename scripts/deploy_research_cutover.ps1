[CmdletBinding()]
param([ValidateSet('Check','Apply','Rollback','ValidateParameters','InspectRecovery','ArchiveFailure','StartMaintenance','Diagnose','DiagnoseRights','LoadLibrary')][string]$Mode='Check')

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$repo=Split-Path -Parent $PSScriptRoot
$live='D:\accio\stock_data'
$runtime='D:\accio\stock-data-runtime'
$workspace=Join-Path $repo 'reports\research-delivery'
$bundle=Join-Path $repo 'reports\ops-repair-20260912\legacy-hotfix'
$package=Join-Path $repo 'reports\closure-20260912\stock-data-research-0.3.10.zip'
$release=Join-Path $runtime 'releases\research-0.3.10-e8bed4e550c81'
$stateDir=Join-Path $runtime 'deployment-20260913'
$journalPath=Join-Path $stateDir 'journal.json'
$python='D:\anaconda\python.exe'
$taskName='StockData-ResearchDaily'
$userName='StockDataResearch'
$userDescription='StockData read-only research; no trading'
$failureArchive='D:\accio\stock-data-runtime.failed-20260913-164649'
$maintenanceDir='D:\accio\stock-data-maintenance-20260914-once'
$maintenancePermit=Join-Path $maintenanceDir 'permit.json'
$zipHash='e8bed4e550c81e44d85863cb3cd6b43bc48dfe633e699c5b6d20ac0e7f3acdb3'
$manifestHash='553892d78e698bb205fb10952b42c857660b5b65ab043a2947f9511b494f9111'
$hotfixHash='9f40772a5285896a68941ad1d32ca42df9f363aeb27cd3712bba2c0387f7443f'
$wrapperHash='3d4a94fa6e63510cb3d1dedecbeeb9cb038c9320718cf1d895b9492cc0e6e218'
$probeHash='c692467bf751d635dfb403acfd35991d5221c215909643844b4107560ff6dffe'
$script:journal=$null
$guards=[Collections.Generic.List[IO.FileStream]]::new()

function Assert-Hash([string]$Path,[string]$Expected) {
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) { throw "Hash changed: $Path" }
}
function Assert-PlainPath([string]$Path) {
    $cursor=[IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse/junction path refused: $cursor"
            }
        }
        $parent=Split-Path -Parent $cursor
        if ($parent -eq $cursor) { break }; $cursor=$parent
    }
}
function Assert-Admin {
    $id=[Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]::new($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script in your Administrator PowerShell window.'
    }
}
function Get-ProbeDiagnostic {
    # Read-only and allowlisted: never export task arguments, environment files,
    # passwords, or full security events. Distinguish invisible from absent.
    $report=[ordered]@{Changes=0;CapturedAt=[DateTimeOffset]::UtcNow.ToString('o');Task=$taskName;Errors=@()}
    try {
        $task=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        $info=Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
        $report.TaskState=[string]$task.State
        $report.Principal=[ordered]@{UserId=$task.Principal.UserId;LogonType=[string]$task.Principal.LogonType;RunLevel=[string]$task.Principal.RunLevel}
        $report.LastRun=$info.LastRunTime.ToString('o')
        $report.Result=[long]$info.LastTaskResult
        $report.ResultHex=('0x{0:X8}' -f [long]$info.LastTaskResult)
        $report.HasRun=($info.LastRunTime.Year -gt 2000 -and $info.LastTaskResult -ne 267011)
        $report.Settings=[ordered]@{Enabled=$task.Settings.Enabled;AllowDemandStart=$task.Settings.AllowDemandStart;DisallowStartIfOnBatteries=$task.Settings.DisallowStartIfOnBatteries;RunOnlyIfNetworkAvailable=$task.Settings.RunOnlyIfNetworkAvailable}
    } catch { $report.Errors+=('task_unavailable: '+$_.Exception.GetType().Name) }
    try { $account=Get-LocalUser -Name $userName -ErrorAction Stop; $report.AccountEnabled=$account.Enabled } catch { $report.Errors+=('account_unavailable: '+$_.Exception.GetType().Name) }
    try {
        $saved=Get-Content -LiteralPath $journalPath -Raw -ErrorAction Stop | ConvertFrom-Json
        $report.JournalStatus=$saved.status; $report.AccountWasCreated=$saved.user_created
    } catch { $report.Errors+=('journal_unavailable: '+$_.Exception.GetType().Name) }
    try {
        $report.OperationalLogEnabled=(Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational' -ErrorAction Stop).IsEnabled
    } catch { $report.Errors+=('task_log_unavailable: '+$_.Exception.GetType().Name) }
    $report.FailedLogons=@()
    try {
        $xpath="*[System[EventID=4625 and TimeCreated[timediff(@SystemTime) <= 604800000]] and EventData[Data[@Name='TargetUserName']='$userName']]"
        foreach ($event in Get-WinEvent -LogName Security -FilterXPath $xpath -MaxEvents 5 -ErrorAction Stop) {
            [xml]$xml=$event.ToXml(); $fields=@{}
            foreach ($entry in $xml.Event.EventData.Data) { $fields[[string]$entry.Name]=[string]$entry.'#text' }
            $report.FailedLogons+=([ordered]@{Time=$event.TimeCreated.ToString('o');Status=$fields.Status;SubStatus=$fields.SubStatus;LogonType=$fields.LogonType;FailureReason=$fields.FailureReason})
        }
    } catch {
        if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $report.SecurityEvidence='no_matching_events_not_proof_of_success' }
        else { $report.Errors+=('security_log_unavailable: '+$_.Exception.GetType().Name) }
    }
    $report.LaunchLogPresent=Test-Path -LiteralPath (Join-Path $runtime 'probe-results\launch.log') -ErrorAction SilentlyContinue
    return $report
}
function Get-BatchRightsDiagnostic {
    Assert-Admin
    # secedit /export only: no /configure and no security-policy changes.
    $diagnosticDir=Join-Path ([IO.Path]::GetTempPath()) ('stockdata-rights-'+[Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $diagnosticDir | Out-Null
    $export=Join-Path $diagnosticDir 'user-rights.inf'
    & "$env:SystemRoot\System32\secedit.exe" /export /cfg $export /areas USER_RIGHTS /log (Join-Path $diagnosticDir 'export.log') /quiet | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Read-only rights export failed. Evidence retained: $diagnosticDir" }
    $account=Get-LocalUser -Name $userName -ErrorAction Stop
    $groups=@()
    foreach ($group in Get-LocalGroup) {
        try {
            if (@(Get-LocalGroupMember -Group $group.Name -ErrorAction Stop | Where-Object {$_.SID.Value -eq $account.SID.Value}).Count) {
                $groups+=([ordered]@{Name=$group.Name;Sid=$group.SID.Value})
            }
        } catch { throw "Cannot establish all local groups; inspect manually. Export retained: $diagnosticDir" }
    }
    # Strip PowerShell provider metadata before ConvertTo-Json; decorated
    # Get-Content strings otherwise serialize megabytes of PSDrive metadata.
    $lines=@([IO.File]::ReadAllLines($export) | Where-Object {$_ -match '^Se(BatchLogonRight|DenyBatchLogonRight)\s*='} | ForEach-Object {$_.ToString()})
    [ordered]@{Changes=0;Account=$userName;Sid=$account.SID.Value;Enabled=$account.Enabled;DirectLocalGroups=$groups;BatchPolicies=$lines;ExportDirectory=$diagnosticDir;Scope='read_only_export_not_effective_token_or_domain_policy_approval'}
}
function Resolve-BatchPolicySid([string]$Identity) {
    # secedit exports both *SIDs and resolved account/group names. Compare
    # canonical SIDs, never trust a matching display name or ignore an unknown
    # identity (especially on the deny side).
    $principal=$Identity.Trim()
    if ([string]::IsNullOrWhiteSpace($principal)) { throw 'Empty batch logon policy identity. No probe started.' }
    try {
        if ($principal.StartsWith('*')) {
            return [Security.Principal.SecurityIdentifier]::new($principal.Substring(1)).Value
        }
        if ($principal -match '^S-\d') {
            return [Security.Principal.SecurityIdentifier]::new($principal).Value
        }
        return ([Security.Principal.NTAccount]::new($principal)).Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {
        throw "Cannot resolve batch logon policy identity '$principal' to a SID. No probe started."
    }
}
function Assert-BatchLogon {
    $rights=Get-BatchRightsDiagnostic
    $allow=@();$deny=@()
    foreach ($line in $rights.BatchPolicies) {
        if ($line -match '^SeBatchLogonRight\s*=\s*(.*)$') { $allow=@(if ($Matches[1].Trim()) { $Matches[1].Split(',') | ForEach-Object {Resolve-BatchPolicySid $_} }) }
        if ($line -match '^SeDenyBatchLogonRight\s*=\s*(.*)$') { $deny=@(if ($Matches[1].Trim()) { $Matches[1].Split(',') | ForEach-Object {Resolve-BatchPolicySid $_} }) }
    }
    $identities=@($rights.Sid,'S-1-1-0','S-1-5-11','S-1-5-113')+@($rights.DirectLocalGroups | ForEach-Object {$_.Sid})
    if (@($deny | Where-Object {$_ -in $identities}).Count -or $rights.Sid -notin $allow) {
        throw 'Batch logon policy is not qualified for the dedicated SID. No probe started. Inspect DiagnoseRights; never elevate the account or bypass deny policies.'
    }
}
function Assert-AccountParameters {
    param(
        [ValidateNotNullOrEmpty()][ValidateLength(1,20)][string]$Name,
        [ValidateNotNullOrEmpty()][ValidateLength(1,48)][string]$Description
    )
    # Inspect the installed cmdlet without invoking account creation. Keep the
    # explicit bounds above so malformed constants fail before any file write.
    $command=Get-Command 'Microsoft.PowerShell.LocalAccounts\New-LocalUser' -ErrorAction Stop
    foreach ($parameter in @('Name','Description')) {
        $value=if ($parameter -eq 'Name') {$Name} else {$Description}
        foreach ($rule in $command.Parameters[$parameter].Attributes) {
            if ($rule -is [Management.Automation.ValidateLengthAttribute] -and
                ($value.Length -lt $rule.MinLength -or $value.Length -gt $rule.MaxLength)) {
                throw "Local account $parameter violates installed cmdlet length limits. No changes made."
            }
        }
    }
}
function Assert-Window([DateTimeOffset]$now=[DateTimeOffset]::UtcNow) {
    $permit=Read-MaintenancePermit
    $start=[DateTimeOffset]::Parse($permit.started_utc)
    $end=[DateTimeOffset]::Parse($permit.expires_utc)
    if (($end-$start).TotalSeconds -ne 3600 -or $now -lt $start -or $now -ge $end) {
        throw 'One-time maintenance window absent, invalid or expired. It cannot be renewed by rerunning.'
    }
}
function Read-MaintenancePermit {
    Assert-PlainPath $maintenanceDir; Assert-PlainPath $maintenancePermit
    if (-not (Test-Path -LiteralPath $maintenancePermit -PathType Leaf)) { throw 'No maintenance permit. Use the interactive retry entrypoint.' }
    foreach ($path in @($maintenanceDir,$maintenancePermit)) {
        $acl=Get-Acl -LiteralPath $path
        if ($path -eq $maintenanceDir -and -not $acl.AreAccessRulesProtected) { throw 'Maintenance directory is not private.' }
        $administrators=$false;$system=$false
        foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow') {
                if ($rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544')) { throw 'Unexpected maintenance permit access.' }
                if ($rule.IdentityReference.Value -eq 'S-1-5-18') {$system=$true}
                if ($rule.IdentityReference.Value -eq 'S-1-5-32-544') {$administrators=$true}
            }
        }
        if (-not $administrators -or -not $system) { throw 'Incomplete maintenance permit ACL.' }
    }
    $permit=Get-Content -LiteralPath $maintenancePermit -Raw | ConvertFrom-Json
    if ($permit.schema -ne 1 -or $permit.scope -ne 'approved_0310_recovery_and_readonly_deployment_once' -or
        $permit.installer_sha256 -ne (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash -or
        $permit.package_sha256 -ne $zipHash) { throw 'Maintenance permit does not match this installer and fixed release.' }
    return $permit
}
function Assert-MaintenanceHostTimezone {
    if ([TimeZoneInfo]::Local.BaseUtcOffset -ne [TimeSpan]::FromHours(8) -or [TimeZoneInfo]::Local.SupportsDaylightSavingTime) { throw 'Expected fixed China UTC+08:00 host timezone.' }
}
function Assert-ScheduleClear([DateTimeOffset]$Start,[DateTimeOffset]$End) {
    if (($End-$Start).TotalSeconds -ne 3600) { throw 'Expected an exact 60-minute scheduling check.' }
    foreach ($task in Get-ScheduledTask -TaskName 'StockData*') {
        if ($task.State -eq 'Disabled') { continue }
        if ($task.State -eq 'Running') { throw "Existing task is running: $($task.TaskName). No maintenance started." }
        $info=Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath
        if (-not $info.NextRunTime -or $info.NextRunTime.Year -le 2000) { throw "Cannot verify next run for $($task.TaskName)." }
        $next=[DateTimeOffset]::new([DateTime]::SpecifyKind($info.NextRunTime,[DateTimeKind]::Unspecified),[TimeSpan]::FromHours(8))
        if ($next -le $End) { throw "Existing task conflicts with the next 60 minutes: $($task.TaskName) at $next. Try a quiet period; no permit created." }
    }
}
function Start-OneTimeMaintenance {
    Assert-Admin; Assert-Idle; Assert-MaintenanceHostTimezone
    Assert-PlainPath $maintenanceDir
    if (Test-Path -LiteralPath $maintenanceDir) { throw 'This one-time authorization was already started or interrupted. Do not delete it or renew it by rerunning.' }
    Assert-RecoveryState
    $now=[DateTimeOffset]::UtcNow; Assert-ScheduleClear $now ($now.AddMinutes(60))
    Write-Output 'Scope: preserve failed directory, deploy fixed 0.3.10, dedicated read-only identity; no trading, no changes to original schedules.'
    $confirmation=Read-Host 'When ready NOW, type START to begin one non-renewable 60-minute window (anything else cancels)'
    if ($confirmation -cne 'START') { throw 'Cancelled. No permit or deployment changes made.' }
    $confirmedAt=[DateTimeOffset]::UtcNow
    try {
        Hold-Guard (Join-Path $live 'kpl_data.duckdb.pipeline.lock.guard')
        Hold-Guard (Join-Path $workspace 'update.guard')
        Hold-Guard (Join-Path $workspace 'publication.guard')
        # Recheck after waiting for human input. Never hold writer locks while
        # waiting at the prompt, and never shift the deadline in later steps.
        Assert-Idle; Assert-RecoveryState
        if (Test-Path -LiteralPath $maintenanceDir) { throw 'Another invocation already consumed this one-time authorization.' }
        $now=$confirmedAt; $end=$now.AddMinutes(60)
        Assert-ScheduleClear $now $end
        Assert-Hash $package $zipHash
        Lock-NewDirectory $maintenanceDir
        $permit=[ordered]@{schema=1;scope='approved_0310_recovery_and_readonly_deployment_once';started_utc=$now.ToString('o');expires_utc=$end.ToString('o');confirmed_by_sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;installer_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash;package_sha256=$zipHash}
        $stream=[IO.File]::Open($maintenancePermit,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try {$bytes=[Text.UTF8Encoding]::new($false).GetBytes(($permit | ConvertTo-Json));$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
        Assert-Window
        [pscustomobject]@{Maintenance='STARTED_ONCE';StartChina=$now.ToOffset([TimeSpan]::FromHours(8)).ToString('o');EndChina=$end.ToOffset([TimeSpan]::FromHours(8)).ToString('o');Renewable=$false} | ConvertTo-Json
    } finally {foreach ($g in $guards) {$g.Dispose()}}
}
function Save-Journal {
    $text=$script:journal | ConvertTo-Json -Depth 12
    # Write-ahead ACL snapshots must reach disk before the permission mutation.
    # A retained .new means an interrupted write; never overwrite its evidence.
    $staged=$journalPath+'.new'
    $stream=[IO.File]::Open($staged,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$bytes=[Text.UTF8Encoding]::new($false).GetBytes($text);$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
    if ([IO.File]::Exists($journalPath)) {[IO.File]::Replace($staged,$journalPath,[NullString]::Value)}
    else {[IO.File]::Move($staged,$journalPath)}
}
function Save-Acl([string]$Path) {
    Assert-PlainPath $Path
    if (-not $script:journal.acls.Contains($Path)) {
        $acl=Get-Acl -LiteralPath $Path
        $script:journal.acls[$Path]=$acl.Sddl
        Save-Journal
    }
}
function Grant-Access([string]$Path,[string]$Rights,[bool]$Children=$false,[string]$Type='Allow') {
    Save-Acl $Path
    $acl=Get-Acl -LiteralPath $Path
    $inherit=if ($Children) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    $rule=[Security.AccessControl.FileSystemAccessRule]::new($script:researchSid,[Security.AccessControl.FileSystemRights]$Rights,$inherit,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]$Type)
    $acl.AddAccessRule($rule); Set-Acl -LiteralPath $Path -AclObject $acl
}
function Lock-NewDirectory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $acl=Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($sidText in @('S-1-5-18','S-1-5-32-544')) {
        $sid=[Security.Principal.SecurityIdentifier]::new($sidText)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Hold-Guard([string]$Path) {
    Assert-PlainPath $Path
    # Permanent carriers already exist. Never delete them or steal metadata.
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
    try { $stream.Lock(0,1) } catch { $stream.Dispose(); throw 'A pipeline/update owns its guard; maintenance stopped.' }
    $guards.Add($stream)
}
function Assert-Idle {
    $running=@(Get-ScheduledTask -TaskName 'StockData-*' | Where-Object { $_.State -eq 'Running' })
    if ($running.Count) { throw 'A StockData task is running; do not stop or kill it.' }
    if (Test-Path -LiteralPath (Join-Path $live 'kpl_data.duckdb.pipeline.lock')) { throw 'Pipeline metadata requires maintenance inspection; do not delete it.' }
}
function Old-Tasks {
    $result=[ordered]@{}
    foreach ($name in @('StockData-Auction','StockData-Intraday','StockData-DailyClose','StockData-MonthlyCompact')) {
        $result[$name]=Export-ScheduledTask -TaskName $name
    }
    return $result
}
function Assert-OldTasks {
    $current=Old-Tasks
    foreach ($name in $script:journal.old_tasks.Keys) {
        if ($current[$name] -ne $script:journal.old_tasks[$name]) { throw "Existing task changed during deployment: $name" }
    }
    if ((Get-ScheduledTask -TaskName 'StockData-MonthlyCompact').State -ne 'Disabled') { throw 'Monthly compact must stay disabled.' }
}
function Restore-Deployment {
    $task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($script:journal.user_created) {
        $existing=Get-LocalUser -Name $userName -ErrorAction Stop
        if ($existing.SID.Value -ne $script:journal.sid) { throw 'Account SID changed; rollback will not disable an unrelated identity.' }
        Disable-LocalUser -Name $userName
    }
    if ($task) {
        if ($task.Principal.UserId -notin @($script:journal.sid, "$env:COMPUTERNAME\$userName", $userName)) { throw 'Unrelated task identity; rollback refused.' }
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        if ($task.State -eq 'Running') { throw 'Task is still running. Disabled future starts; do not force stop. Retry rollback after it exits.' }
    }
    foreach ($name in $script:journal.files.Keys) {
        $entry=$script:journal.files[$name]; $target=Join-Path $live $name
        $actual=(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($actual -eq $entry.before_sha256) { continue }
        if ($actual -ne $entry.after_sha256) { throw "Live file changed independently; do not overwrite: $name" }
        $backup=Join-Path (Join-Path $stateDir 'before') $name
        Assert-Hash $backup $entry.before_sha256
        Copy-Item -LiteralPath $backup -Destination $target -Force
        Assert-Hash $target $entry.before_sha256
    }
    $paths=@($script:journal.acls.Keys) | Sort-Object Length -Descending
    foreach ($path in $paths) {
        Assert-PlainPath $path
        $acl=Get-Acl -LiteralPath $path
        $acl.SetSecurityDescriptorSddlForm($script:journal.acls[$path],[Security.AccessControl.AccessControlSections]::Access)
        Set-Acl -LiteralPath $path -AclObject $acl
    }
    $script:journal.status='rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained'
    Save-Journal
}

function Assert-RecoveryState {
    # This recovery is ONLY for the observed pre-account failure, not a general
    # resume/cleanup switch for later partial installations.
    if ([IO.Path]::GetFullPath($runtime) -ne 'D:\accio\stock-data-runtime' -or
        [IO.Path]::GetFullPath($failureArchive) -ne 'D:\accio\stock-data-runtime.failed-20260913-164649') {
        throw 'Unexpected recovery paths; no archive permitted.'
    }
    Assert-PlainPath $runtime; Assert-PlainPath $failureArchive
    if (-not (Test-Path -LiteralPath $runtime -PathType Container) -or (Test-Path -LiteralPath $failureArchive)) {
        throw 'Recovery requires original directory and absent archive destination; do not overwrite.'
    }
    $saved=Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
    if ($saved.status -ne 'rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained' -or
        $saved.user_created -isnot [bool] -or $saved.user_created -or $saved.sid -ne '' -or
        $saved.installer_sha256 -ne '8924c867edadf56f86da7c192560bd5340cda2ccc253d81e03b79fff1b91a16e' -or
        @($saved.acls.PSObject.Properties).Count -ne 0) {
        throw 'Journal is not the approved pre-account rollback; manual inspection required.'
    }
    if ((Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) -or
        (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        throw 'Research account or task exists; do not archive or recreate it.'
    }
    # The failure preceded every Grant-Access call; a nonempty ACL journal is
    # explicitly rejected above. New private directories must remain private.
    foreach ($private in @($runtime,$stateDir)) {
        $acl=Get-Acl -LiteralPath $private
        if (-not $acl.AreAccessRulesProtected) { throw 'Recovery directory ACL inheritance changed.' }
        $allowed=@()
        foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow') {
                if ($rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544')) { throw 'Recovery directory has an unexpected allow rule.' }
                if (($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl) {
                    $allowed+=$rule.IdentityReference.Value
                }
            }
        }
        if ('S-1-5-18' -notin $allowed -or 'S-1-5-32-544' -notin $allowed) { throw 'Recovery administrative ACL incomplete.' }
    }
    Assert-Hash (Join-Path $bundle 'manifest.json') $hotfixHash
    $expected=Get-Content -LiteralPath (Join-Path $bundle 'manifest.json') -Raw | ConvertFrom-Json
    if (@($saved.files.PSObject.Properties).Count -ne 3 -or @($expected.files.PSObject.Properties).Count -ne 3) { throw 'Unexpected backup file set.' }
    foreach ($p in $saved.files.PSObject.Properties) {
        $entry=$expected.files.PSObject.Properties[$p.Name]
        if (-not $entry -or $p.Value.before_sha256 -ne $entry.Value.before_sha256 -or $p.Value.after_sha256 -ne $entry.Value.after_sha256) { throw 'Backup manifest mismatch.' }
        Assert-Hash (Join-Path $live $p.Name) $entry.Value.before_sha256
        Assert-Hash (Join-Path (Join-Path $stateDir 'before') $p.Name) $entry.Value.before_sha256
    }
    $tasks=Old-Tasks
    if (@($saved.old_tasks.PSObject.Properties).Count -ne $tasks.Count) { throw 'Unexpected original task set.' }
    foreach ($p in $saved.old_tasks.PSObject.Properties) {
        if (-not $tasks.Contains($p.Name) -or $tasks[$p.Name] -ne $p.Value) { throw 'Original task changed; recovery stopped.' }
    }
    if ((Get-ScheduledTask -TaskName 'StockData-MonthlyCompact').State -ne 'Disabled') { throw 'Monthly compact must remain disabled.' }
}

function Recovery-TreeFingerprint([string]$Root) {
    # Bounded traversal, refusing links before descent. Keep contents (including
    # protected credentials) out of tool output; compare hashes and SDDL only.
    $pending=[Collections.Generic.Stack[string]]::new(); $pending.Push($Root)
    $records=[Collections.Generic.List[string]]::new(); $bytes=0L
    while ($pending.Count) {
        $path=$pending.Pop(); $item=Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Recovery tree contains a link; stopped.' }
        if ($records.Count -ge 1000) { throw 'Recovery tree exceeds approved bounded inventory.' }
        $relative=$path.Substring($Root.Length)
        $sddl=(Get-Acl -LiteralPath $path).Sddl
        if ($item.PSIsContainer) {
            $records.Add("D|$relative|$sddl")
            foreach ($child in Get-ChildItem -LiteralPath $path -Force) { $pending.Push($child.FullName) }
        } else {
            $bytes+=$item.Length
            if ($item.Length -gt 100MB -or $bytes -gt 500MB) { throw 'Recovery inventory exceeds size budget.' }
            $records.Add("F|$relative|$sddl|$($item.Length)|$((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash)")
        }
    }
    return (@($records | Sort-Object) -join "`n")
}

# Shared primitives for the separately scoped retained-account probe. Loading
# definitions must not run preflight, create a permit, or enter legacy Apply.
if ($Mode -eq 'LoadLibrary') { return }
if ($Mode -ne 'Rollback') { Assert-AccountParameters -Name $userName -Description $userDescription }
if ($Mode -eq 'ValidateParameters') {
    [pscustomobject]@{ParameterValidation='PASS';DescriptionLength=$userDescription.Length;Changes=0;DeploymentPreflightPerformed=$false} | ConvertTo-Json
    exit 0
}
if ($Mode -eq 'Diagnose') { Get-ProbeDiagnostic | ConvertTo-Json -Depth 6; exit 0 }
if ($Mode -eq 'DiagnoseRights') {
    $reportDir=Join-Path $repo 'reports\repair-20260916'
    Assert-PlainPath $reportDir
    if (-not (Test-Path -LiteralPath $reportDir)) { New-Item -ItemType Directory -Path $reportDir | Out-Null }
    $reportPath=Join-Path $reportDir ('batch-rights-'+[Guid]::NewGuid().ToString('N')+'.json')
    try { $diagnostic=Get-BatchRightsDiagnostic } catch { $diagnostic=[ordered]@{Changes=0;Error=$_.Exception.Message} }
    [IO.File]::WriteAllText($reportPath,($diagnostic | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
    Write-Output "DIAGNOSTIC_SAVED: $reportPath ; system changes=0. No need to paste the full output."
    exit 0
}
if ($Mode -eq 'StartMaintenance') { Start-OneTimeMaintenance; exit 0 }
if ($Mode -in @('InspectRecovery','ArchiveFailure')) {
    Assert-Admin; Assert-Idle
    if ($Mode -eq 'ArchiveFailure') { Assert-Window }
    try {
        Hold-Guard (Join-Path $live 'kpl_data.duckdb.pipeline.lock.guard')
        Hold-Guard (Join-Path $workspace 'update.guard')
        Hold-Guard (Join-Path $workspace 'publication.guard')
        Assert-RecoveryState
        $before=Recovery-TreeFingerprint $runtime
        if ($Mode -eq 'InspectRecovery') {
            Write-Output 'RECOVERY_CHECK_PASS: pre-account rollback verified; private ACLs, backups and original tasks verified; Changes=0.'
        } else {
            Assert-Window
            # Exact same-parent rename; no recursive deletion, overwrite, ACL
            # change or copy across volumes. Both resolved targets checked above.
            Rename-Item -LiteralPath $runtime -NewName (Split-Path -Leaf $failureArchive) -ErrorAction Stop
            $after=Recovery-TreeFingerprint $failureArchive
            if ($before -cne $after) { throw "Archive verification failed; retained at $failureArchive. Do not apply." }
            Write-Output "ARCHIVE_COMPLETE: $failureArchive; every file and ACL preserved. No installation performed."
        }
    } finally { foreach ($g in $guards) { $g.Dispose() } }
    exit 0
}
foreach ($path in @($repo,$live,$runtime,$workspace,$bundle,$python)) { Assert-PlainPath $path }
if ($Mode -eq 'Rollback') {
    Assert-Admin; Assert-Idle
    $loaded=Get-Content -Raw -LiteralPath $journalPath | ConvertFrom-Json
    $script:journal=[ordered]@{status=$loaded.status;sid=$loaded.sid;user_created=$loaded.user_created;acls=[ordered]@{};files=[ordered]@{};old_tasks=[ordered]@{}}
    foreach ($key in @('acls','files','old_tasks')) { foreach ($p in $loaded.$key.PSObject.Properties) { $script:journal[$key][$p.Name]=$p.Value } }
    # A privileged journal must never be replaced with arbitrary restore paths.
    $validFiles=@('trade_system/review_web.py','scripts/audit_daily_review_artifact.py','scripts/check_kpl_connectivity.py')
    foreach ($name in $script:journal.files.Keys) { if ($name -notin $validFiles) { throw 'Unexpected restore file' } }
    foreach ($path in $script:journal.acls.Keys) {
        $full=[IO.Path]::GetFullPath($path)
        if ($full -ne 'D:\anaconda' -and $full -ne (Join-Path $live 'kpl_data.duckdb') -and -not $full.StartsWith($workspace+'\',[StringComparison]::OrdinalIgnoreCase) -and $full -ne $workspace -and -not $full.StartsWith($runtime+'\',[StringComparison]::OrdinalIgnoreCase) -and $full -ne $runtime -and -not $full.StartsWith((Join-Path $repo 'data\research-inputs')+'\',[StringComparison]::OrdinalIgnoreCase) -and $full -ne (Join-Path $repo 'data\research-inputs')) { throw 'Unexpected ACL restore target' }
    }
    try { Hold-Guard (Join-Path $live 'kpl_data.duckdb.pipeline.lock.guard'); Hold-Guard (Join-Path $workspace 'update.guard'); Restore-Deployment } finally { foreach ($g in $guards) { $g.Dispose() } }
    Write-Output 'ROLLBACK_COMPLETE: files and ACLs restored; new account/task disabled; data and logs retained.'
    exit 0
}

Assert-Hash $package $zipHash
Assert-Hash (Join-Path $bundle 'manifest.json') $hotfixHash
Assert-Hash (Join-Path $PSScriptRoot 'run_research_daily.ps1') $wrapperHash
Assert-Hash (Join-Path $repo 'tools\v2\deployment_probe.py') $probeHash
$manifest=Get-Content -Raw -LiteralPath (Join-Path $bundle 'manifest.json') | ConvertFrom-Json
foreach ($p in $manifest.files.PSObject.Properties) {
    Assert-Hash (Join-Path $live $p.Name) $p.Value.before_sha256
    Assert-Hash (Join-Path $bundle $p.Name) $p.Value.after_sha256
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Research task already exists: inspect it; never overwrite automatically.' }
if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) { throw 'Research identity already exists: inspect it; never reset an unrelated account.' }
if (Test-Path -LiteralPath $runtime) { throw 'Runtime directory already exists: inspect previous deployment; never overwrite it.' }
if ((Get-ScheduledTask -TaskName 'StockData-MonthlyCompact').State -ne 'Disabled') { throw 'Monthly compact must stay disabled.' }
Assert-Idle
if ($Mode -eq 'Check') {
    $admin=([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $window=if (Test-Path -LiteralPath $maintenancePermit) {Read-MaintenancePermit} else {$null}
    [pscustomobject]@{Preflight='PASS_read_only';Administrator=$admin;Changes=0;WindowStarted=($null -ne $window);Window=$window;Task=$taskName;OriginalFirstScheduledRun='2026-09-14 18:30 +08:00'} | ConvertTo-Json
    exit 0
}
Assert-Admin; Assert-Window
if ([TimeZoneInfo]::Local.BaseUtcOffset -ne [TimeSpan]::FromHours(8) -or [TimeZoneInfo]::Local.SupportsDaylightSavingTime) { throw 'Task host must use fixed China UTC+08:00 time; do not change it automatically.' }
$plainPassword=$null; $securePassword=$null
try {
    Hold-Guard (Join-Path $live 'kpl_data.duckdb.pipeline.lock.guard')
    Hold-Guard (Join-Path $workspace 'update.guard')
    Hold-Guard (Join-Path $workspace 'publication.guard')
    Lock-NewDirectory $runtime; Lock-NewDirectory $stateDir
    $script:journal=[ordered]@{status='preparing';sid='';user_created=$false;installer_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash;acls=[ordered]@{};files=[ordered]@{};old_tasks=(Old-Tasks)}
    Save-Journal
    foreach ($p in $manifest.files.PSObject.Properties) {
        $backup=Join-Path (Join-Path $stateDir 'before') $p.Name
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $live $p.Name) -Destination $backup
        Assert-Hash $backup $p.Value.before_sha256
        $script:journal.files[$p.Name]=$p.Value; Save-Journal
    }
    New-Item -ItemType Directory -Path $release -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($package)
    try {
        foreach ($entry in $zip.Entries) {
            $target=[IO.Path]::GetFullPath((Join-Path $release $entry.FullName))
            if (-not $target.StartsWith($release+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe ZIP member' }
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$target,$false)
        }
    } finally { $zip.Dispose() }
    Assert-Hash (Join-Path $release 'research-release.json') $manifestHash
    $runScripts=Join-Path $runtime 'scripts'; New-Item -ItemType Directory -Path $runScripts | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run_research_daily.ps1') -Destination $runScripts
    Copy-Item -LiteralPath (Join-Path $repo 'tools\v2\deployment_probe.py') -Destination $runScripts
    Assert-Hash (Join-Path $runScripts 'deployment_probe.py') $probeHash
    $configDir=Join-Path $runtime 'config'; New-Item -ItemType Directory -Path $configDir | Out-Null
    $environment=Join-Path $configDir 'research.env'
    $allow=@('HITHINK_FINANCE_API_KEY','XIAODEFA_TOKEN','TUSHARE_XIAODEFA_TOKEN','XIAODEFA_URL')
    $values=@{}
    foreach ($line in Get-Content -LiteralPath (Join-Path $live '.env')) {
        if ($line -match '^\s*([A-Z_]+)\s*=(.*)$' -and $Matches[1] -in $allow) { $values[$Matches[1]]=$Matches[2].Trim() }
    }
    if (-not $values['HITHINK_FINANCE_API_KEY'] -or (-not $values['XIAODEFA_TOKEN'] -and -not $values['TUSHARE_XIAODEFA_TOKEN'])) { throw 'Required market-only credentials missing' }
    [IO.File]::WriteAllLines($environment,@($values.Keys | Sort-Object | ForEach-Object { $_+'='+$values[$_] }),[Text.UTF8Encoding]::new($false))
    $random=[byte[]]::new(48); $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($random) } finally { $rng.Dispose() }
    $plainPassword='Aa1!'+(-join @($random | ForEach-Object { [char](65+($_ % 26)) }))
    $securePassword=ConvertTo-SecureString $plainPassword -AsPlainText -Force
    $newUser=New-LocalUser -Name $userName -Password $securePassword -Description $userDescription -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword
    $script:researchSid=$newUser.SID
    $script:journal.sid=$newUser.SID.Value; $script:journal.user_created=$true; Save-Journal
    $usersGroup=Get-LocalGroup -SID 'S-1-5-32-545'
    if (-not (@(Get-LocalGroupMember -Group $usersGroup | Where-Object { $_.SID -eq $newUser.SID }).Count)) {
        Add-LocalGroupMember -Group $usersGroup -Member $newUser
    }
    Grant-Access $runtime 'ReadAndExecute'
    Grant-Access $release 'ReadAndExecute' $true
    Grant-Access $runScripts 'ReadAndExecute' $true
    Grant-Access $configDir 'ReadAndExecute' $true
    Grant-Access 'D:\anaconda' 'ReadAndExecute' $true
    Grant-Access (Join-Path $live 'kpl_data.duckdb') 'Read'
    Grant-Access (Join-Path $live 'kpl_data.duckdb') 'Write,Delete,ChangePermissions,TakeOwnership' $false 'Deny'
    Grant-Access (Join-Path $repo 'data\research-inputs') 'ReadAndExecute' $true
    Grant-Access $workspace 'Modify' $true
    # Empty containers/guard are not fabricated human records. The automated
    # identity may read notes for review but cannot write them or take its lock.
    if (-not (Test-Path -LiteralPath (Join-Path $workspace 'notes'))) { New-Item -ItemType Directory -Path (Join-Path $workspace 'notes') | Out-Null }
    if (-not (Test-Path -LiteralPath (Join-Path $workspace 'judgement.guard'))) {
        $noteGuard=[IO.File]::Open((Join-Path $workspace 'judgement.guard'),[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
        $noteGuard.Dispose()
    }
    foreach ($name in @('builds','price-studies','retraining','notes','judgement.guard','research-current.json','research-candidate.json','price-study-current.json','workspace-config.json')) {
        $path=Join-Path $workspace $name
        if (Test-Path -LiteralPath $path) { Grant-Access $path 'Write,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership' (Test-Path -LiteralPath $path -PathType Container) 'Deny' }
    }
    $probeRoot=Join-Path $runtime 'probe-results'; New-Item -ItemType Directory -Path $probeRoot | Out-Null
    Grant-Access $probeRoot 'Modify' $true
    $nonce=[Guid]::NewGuid().ToString('N'); $probeOutput=Join-Path $probeRoot $nonce
    $arguments='-I -X utf8 -B "'+(Join-Path $runScripts 'deployment_probe.py')+'" --release "'+$release+'" --workspace "'+$workspace+'" --database "'+(Join-Path $live 'kpl_data.duckdb')+'" --output "'+$probeOutput+'" --nonce '+$nonce+' --sid '+$newUser.SID.Value+' --environment "'+$environment+'"'
    # Exercise the same PowerShell -File policy and identity as the final job.
    # A Python-only probe would miss an unsigned-script policy rejection.
    $probeLauncher=Join-Path $runScripts 'identity-probe.ps1'
    $probeBody="`$ErrorActionPreference='Stop'`r`n`$env:KPL_ENV_FILE='"+$environment+"'`r`n& '"+$python+"' "+$arguments+" *> '"+(Join-Path $probeRoot 'launch.log')+"'`r`nexit `$LASTEXITCODE`r`n"
    [IO.File]::WriteAllText($probeLauncher,$probeBody,[Text.UTF8Encoding]::new($false))
    $action=New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -NonInteractive -File "'+$probeLauncher+'"') -WorkingDirectory $runtime
    $settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
    Register-ScheduledTask -TaskName $taskName -Action $action -Settings $settings -User "$env:COMPUTERNAME\$userName" -Password $plainPassword -RunLevel Limited -Description 'Initial offline permission probe; no daily trigger until verified' | Out-Null
    Assert-BatchLogon
    Start-ScheduledTask -TaskName $taskName
    $deadline=(Get-Date).AddMinutes(4)
    do { Start-Sleep -Seconds 2; $task=Get-ScheduledTask -TaskName $taskName; $resultExists=Test-Path -LiteralPath (Join-Path $probeOutput 'result.json') } while ((-not $resultExists -or $task.State -eq 'Running') -and (Get-Date) -lt $deadline)
    if (-not $resultExists -or $task.State -eq 'Running' -or (Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult -ne 0) { throw 'Dedicated identity probe failed or timed out; daily trigger was not enabled.' }
    $probe=Get-Content -Raw -LiteralPath (Join-Path $probeOutput 'result.json') | ConvertFrom-Json
    if (-not $probe.passed -or $probe.nonce -ne $nonce -or $probe.sid -ne $newUser.SID.Value) { throw 'Invalid identity probe receipt' }
    Assert-Window; Assert-OldTasks
    foreach ($p in $manifest.files.PSObject.Properties) {
        Assert-Hash (Join-Path $live $p.Name) $p.Value.before_sha256
        Copy-Item -LiteralPath (Join-Path $bundle $p.Name) -Destination (Join-Path $live $p.Name) -Force
        Assert-Hash (Join-Path $live $p.Name) $p.Value.after_sha256
    }
    $reviewDir=Join-Path $stateDir 'post-apply-review'
    New-Item -ItemType Directory -Path $reviewDir | Out-Null
    & $python -X utf8 -B (Join-Path $live 'scripts\generate_daily_review_web.py') --db (Join-Path $live 'kpl_data.duckdb') --trade-date $probe.prediction_date --out (Join-Path $reviewDir 'daily_review_latest.html') *> (Join-Path $stateDir 'post-apply-render.log')
    if ($LASTEXITCODE -ne 0) { throw 'Post-apply real database read-only rendering failed' }
    & $python -X utf8 -B (Join-Path $live 'scripts\audit_daily_review_artifact.py') --db (Join-Path $live 'kpl_data.duckdb') --date $probe.prediction_date --html (Join-Path $reviewDir 'daily_review_latest.html') --out (Join-Path $reviewDir 'audit.md') *> (Join-Path $stateDir 'post-apply-audit.log')
    if ($LASTEXITCODE -ne 0) { throw 'Post-apply artifact audit failed; do not enable daily trigger' }
    Assert-Window
    $arguments='-NoProfile -NonInteractive -File "'+(Join-Path $runScripts 'run_research_daily.ps1')+'" -Python "'+$python+'" -Workspace "'+$workspace+'" -ReleaseDirectory "'+$release+'" -ReleaseManifestSha256 '+$manifestHash+' -EnvironmentFile "'+$environment+'"'
    $action=New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $arguments -WorkingDirectory $runtime
    $trigger=New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '18:30'
    $trigger.StartBoundary='2026-09-14T18:30:00+08:00'
    Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
    Assert-OldTasks
    $script:journal.status='installed_offline_identity_probe_passed_first_real_daily_run_pending'; $script:journal.probe=$probeOutput; Save-Journal
    Write-Output "INSTALL_COMPLETE: dedicated identity verified; first real daily run pending. Receipt: $journalPath"
} catch {
    $failure=$_.Exception.Message
    if ($script:journal) {
        # Preserve the actual pre-rollback account/task state; rollback disables
        # both and would otherwise erase the distinction from launch failure.
        try {
            $script:journal['failure_diagnostic']=Get-ProbeDiagnostic
            Save-Journal
            $script:journal.failure_diagnostic | ConvertTo-Json -Depth 6 | Write-Output
        } catch { Write-Warning 'Unable to retain pre-rollback diagnostic; original failure remains authoritative.' }
        try { Restore-Deployment } catch { Write-Warning ('Rollback needs inspection: '+$_.Exception.Message) }
    }
    throw ('Deployment stopped: '+$failure+'. Inspect retained journal; do not rerun Apply blindly.')
} finally {
    $plainPassword=$null
    if ($securePassword) { $securePassword.Dispose() }
    foreach ($g in $guards) { $g.Dispose() }
}
