[CmdletBinding()]
param([ValidateSet('Check','Apply','Rollback')][string]$Mode='Check')

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
function Assert-Window([DateTimeOffset]$now=[DateTimeOffset]::UtcNow) {
    if ($now -lt [DateTimeOffset]::Parse('2026-09-13T16:00:00+08:00') -or $now -ge [DateTimeOffset]::Parse('2026-09-13T17:00:00+08:00')) {
        throw 'Outside approved maintenance window: 2026-09-13 16:00-17:00 China time. No apply permitted.'
    }
}
function Save-Journal {
    $text=$script:journal | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($journalPath+'.new',$text,[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath ($journalPath+'.new') -Destination $journalPath -Force
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
    [pscustomobject]@{Preflight='PASS_read_only';Administrator=$admin;Changes=0;ApplyWindow='2026-09-13 16:00-17:00 +08:00';Task=$taskName;FirstScheduledRun='2026-09-14 18:30 +08:00'} | ConvertTo-Json
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
    $newUser=New-LocalUser -Name $userName -Password $securePassword -Description 'StockData read-only research publisher; no broker authority' -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword
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
        try { Restore-Deployment } catch { Write-Warning ('Rollback needs inspection: '+$_.Exception.Message) }
    }
    throw ('Deployment stopped: '+$failure+'. Inspect retained journal; do not rerun Apply blindly.')
} finally {
    $plainPassword=$null
    if ($securePassword) { $securePassword.Dispose() }
    foreach ($g in $guards) { $g.Dispose() }
}
