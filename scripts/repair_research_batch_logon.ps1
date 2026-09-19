[CmdletBinding()]
param([ValidateSet('Check','Apply','PrepareAccount','ConfigureAccess')][string]$Mode='Check')
$ErrorActionPreference='Stop'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Use the Administrator PowerShell window. No changes made.'
}
$account=Get-LocalUser -Name 'StockDataResearch' -ErrorAction Stop
if ($account.SID.Value -ne 'S-1-5-21-3027070730-734606845-1610825463-1006' -or $account.Enabled) {
    throw 'Account differs from the reviewed disabled identity.'
}
$tasks=@(Get-ScheduledTask | Where-Object TaskName -like 'StockData-*')
if (@($tasks | Where-Object State -ne 'Disabled').Count) {
    throw 'All stock-data tasks must remain disabled during account preparation.'
}
$task=$tasks | Where-Object TaskName -eq 'StockData-ResearchDaily'
if ($task -and ($task.TaskPath -ne '\' -or $task.Principal.UserId -notin @($account.Name,$account.SID.Value,"$env:COMPUTERNAME\StockDataResearch"))) {
    throw 'Research task identity differs; no changes made.'
}
$repo=Split-Path -Parent $PSScriptRoot
$helper=Join-Path $repo 'tools\v2\repair_batch_logon.py'
& 'D:\anaconda\python.exe' -I -X utf8 -B $helper
if ($LASTEXITCODE -ne 0) { throw 'Read-only batch rights check failed.' }
if ($Mode -eq 'Check') { exit 0 }
if ($Mode -eq 'ConfigureAccess') {
    Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class StockDataDacl { [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool SetFileSecurityW(string path, uint information, byte[] descriptor); }'
    $root='D:\accio\stock_data-runtime';$workspace=Join-Path $root 'workspace'
    $release=Join-Path $root 'research-release'
    $runtime=Join-Path $repo 'reports\deployment-0314-20260916\relocated-runtime\python-research'
    $environment=Join-Path $repo '.env';$nonce=[Guid]::NewGuid().ToString('N')
    $receipt=Join-Path $repo ('reports\handover-20260917\owned-data-handover\access-probe-'+$nonce+'.json')
    $record=[ordered]@{status='prepared';sid=$account.SID.Value;before_acl=@();task_created=$false;account_enabled=$false;probe_nonce=$nonce}
    $probeName='StockData-PermissionProbe'
    if (Get-ScheduledTask -TaskName $probeName -ErrorAction SilentlyContinue) {throw 'Prior probe requires inspection.'}
    function Save-AccessState { $record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receipt -Encoding UTF8 }
    function Protect-Path([string]$Path,[string]$Rights='Write,Delete,DeleteSubdirectoriesAndFiles',[bool]$Children=$true) {
        $resolved=(Resolve-Path -LiteralPath $Path).Path
        if ($resolved -notin @('D:\','D:\accio') -and -not $resolved.StartsWith('D:\accio\',[StringComparison]::OrdinalIgnoreCase)) {throw 'ACL target outside approved roots'}
        if ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Reparse point refused'}
        $acl=Get-Acl -LiteralPath $resolved
        $needed=if ($Rights -eq 'DeleteSubdirectoriesAndFiles') {64} elseif ($Rights -eq 'Read,Write,Delete') {196751} else {65606}
        $denied=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object {
            $_.IdentityReference.Value -eq $account.SID.Value -and $_.AccessControlType -eq 'Deny' -and
            (([int]$_.FileSystemRights -band $needed) -eq $needed) -and
            (-not $Children -or -not (Get-Item -LiteralPath $resolved).PSIsContainer -or ([int]$_.InheritanceFlags -band 3) -eq 3)
        })
        if ($denied.Count) {return}
        $before=@{path=$resolved;sddl=$acl.Sddl};$record.before_acl+= $before
        # Append before each mutation; avoid repeatedly serializing every runtime ACL.
        [IO.File]::AppendAllText($receipt+'.acl.jsonl',($before | ConvertTo-Json -Compress)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
        $inherit=if ($Children -and (Get-Item -LiteralPath $resolved).PSIsContainer) {'ContainerInherit,ObjectInherit'} else {'None'}
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($account.SID,[Security.AccessControl.FileSystemRights]$needed,$inherit,'None','Deny'))
        # SetFileSecurity changes only this DACL. Existing children are enumerated explicitly below.
        if (-not [StockDataDacl]::SetFileSecurityW($resolved,4,$acl.GetSecurityDescriptorBinaryForm())) {throw ('DACL update failed: '+$resolved+' code='+[Runtime.InteropServices.Marshal]::GetLastWin32Error())}
    }
    try {
        Save-AccessState
        foreach ($path in @($root,$repo,'D:\accio\stock_data')) {Protect-Path $path 'Write,Delete,DeleteSubdirectoriesAndFiles' $false}
        foreach ($tree in @((Join-Path $root 'market'),$release,$runtime,(Join-Path (Split-Path $runtime) 'python-collector'))) {
            Protect-Path $tree
            Get-ChildItem -LiteralPath $tree -Recurse -Force | ForEach-Object {Protect-Path $_.FullName 'Write,Delete,DeleteSubdirectoriesAndFiles' $_.PSIsContainer}
        }
        # Protect the sealed source closure and its parents, not historical caches or test environments.
        $contract=Get-Content -LiteralPath (Join-Path $repo 'reports\handover-20260917\owned-data-handover\account-flow-collection-contract.json') -Raw | ConvertFrom-Json
        $plan=Get-Content -LiteralPath (Join-Path $repo 'reports\handover-20260917\owned-data-handover\account-flow-task-proposal.json') -Raw | ConvertFrom-Json
        $members=@($contract.files.PSObject.Properties.Name)+@($plan.AdapterFiles.PSObject.Properties.Name)+@('tools/v2/deployment_probe.py')
        $parents=@{}
        foreach ($member in $members) {
            $path=[IO.Path]::GetFullPath((Join-Path $repo $member))
            if (-not $path.StartsWith($repo+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Source closure escaped root'}
            Protect-Path $path 'Write,Delete,DeleteSubdirectoriesAndFiles' $false
            $parent=Split-Path $path
            while ($parent -ne $repo) {$parents[$parent]=$true;$parent=Split-Path $parent}
        }
        foreach ($path in $parents.Keys) {Protect-Path $path 'Write,Delete,DeleteSubdirectoriesAndFiles' $false}
        Protect-Path 'D:\accio\stock_data\kpl_data.duckdb' 'Write,Delete,DeleteSubdirectoriesAndFiles' $false
        if (-not (Test-Path -LiteralPath $environment)) {
            New-Item -ItemType File -Path $environment | Out-Null
            $record.environment_created=$true;Save-AccessState
        }
        # Protect before copying approved provider keys; never serialize credentials to receipts.
        Protect-Path 'D:\accio\stock_data\.env' 'Read,Write,Delete'
        Protect-Path $environment 'Write,Delete,DeleteSubdirectoriesAndFiles' $false
        $ownerSid=(Get-Acl -LiteralPath $repo).GetOwner([Security.Principal.SecurityIdentifier]).Value
        & "$env:WINDIR\System32\icacls.exe" $environment /inheritance:r /grant:r ('*'+$ownerSid+':(F)') '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' ('*'+$account.SID.Value+':(R)') | Out-Null
        if ($LASTEXITCODE -ne 0) {throw 'Provider environment protection failed'}
        if ($record.environment_created) {
            $lines=[IO.File]::ReadAllLines('D:\accio\stock_data\.env') | Where-Object {$_ -match '^\s*(KPL_API_KEY|KPL_API_BASE|HITHINK_FINANCE_API_KEY|XIAODEFA_TOKEN)\s*='}
            [IO.File]::WriteAllLines($environment,$lines,[Text.UTF8Encoding]::new($false));$lines=$null
        }
        $notes=Join-Path $workspace 'notes';$guard=Join-Path $workspace 'judgement.guard'
        if (-not (Test-Path -LiteralPath $notes)) {New-Item -ItemType Directory -Path $notes | Out-Null}
        if (-not (Test-Path -LiteralPath $guard)) {New-Item -ItemType File -Path $guard | Out-Null}
        foreach ($path in @($notes,$guard,(Join-Path $workspace 'workspace-config.json'))) {Protect-Path $path}
        Protect-Path $workspace 'DeleteSubdirectoriesAndFiles' $false
        $manifest=(Get-FileHash -LiteralPath (Join-Path $release 'research-release.json') -Algorithm SHA256).Hash.ToLowerInvariant()
        $output=Join-Path $workspace ('permission-probe-'+$nonce);$log=$output+'.log'
        $probe=Join-Path $repo 'tools\v2\deployment_probe.py'
        $probeArgs=@('-I','-X','utf8','-B',$probe,'--release',$release,'--workspace',$workspace,'--database',(Join-Path $root 'market\kpl_data.duckdb'),'--output',$output,'--nonce',$nonce,'--sid',$account.SID.Value,'--environment',$environment,'--expected-date','2026-09-18','--manifest-sha256',$manifest,'--runtime',$runtime)
        $quoted=@($probeArgs | ForEach-Object {"'"+$_.Replace("'","''")+"'"}) -join ' '
        $command="& '"+(Join-Path $runtime 'python.exe')+"' "+$quoted+" > '"+$log+"' 2>&1; exit `$LASTEXITCODE"
        $action=New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -NonInteractive -Command "'+$command+'"') -WorkingDirectory $repo
        $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $credential=Get-Credential -UserName "$env:COMPUTERNAME\StockDataResearch" -Message 'Enter the NEW StockDataResearch password for an offline permission probe. No daily pipeline will start.'
        if (-not $credential -or $credential.UserName -ne "$env:COMPUTERNAME\StockDataResearch" -or $credential.Password.Length -eq 0) {throw 'Cancelled or wrong identity'}
        Enable-LocalUser -Name $account.Name;$record.account_enabled=$true;Save-AccessState
        Register-ScheduledTask -TaskName $probeName -TaskPath '\' -Action $action -Settings $settings -User $credential.UserName -Password $credential.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
        $record.task_created=$true;$record.output=$output;$record.log=$log;Save-AccessState
        Start-ScheduledTask -TaskName $probeName
        $deadline=(Get-Date).AddMinutes(3)
        do {Start-Sleep -Seconds 2;$state=(Get-ScheduledTask -TaskName $probeName).State;$info=Get-ScheduledTaskInfo -TaskName $probeName} while ((($state -eq 'Running') -or $info.LastRunTime.Year -lt 2026) -and (Get-Date) -lt $deadline)
        $record.task_result=$info.LastTaskResult
        if ($state -eq 'Running') {throw 'Probe still running; retained for inspection, no forced stop.'}
        if ($info.LastTaskResult -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $output 'result.json'))) {throw 'Real identity probe failed; inspect retained log.'}
        $result=Get-Content -LiteralPath (Join-Path $output 'result.json') -Raw | ConvertFrom-Json
        if (-not $result.passed -or $result.sid -ne $account.SID.Value -or $result.nonce -ne $nonce) {throw 'Probe receipt identity mismatch.'}
        $record.status='real_identity_permissions_verified';Save-AccessState
        Unregister-ScheduledTask -TaskName $probeName -Confirm:$false
        $record.task_created=$false;$record.task_removed=$true;Save-AccessState
        $plan=Join-Path $repo 'reports\handover-20260917\owned-data-handover\admin-task-proposal.json'
        $planHash=(Get-FileHash -LiteralPath $plan).Hash
        $window=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow,'China Standard Time')
        $common=@{Plan=$plan;PlanSha256=$planHash;Credential=$credential;WindowStart=$window.ToString('yyyy-MM-ddTHH:mm:sszzz');WindowEnd=$window.AddMinutes(30).ToString('yyyy-MM-ddTHH:mm:sszzz')}
        $installer=Join-Path $repo 'scripts\deploy_current_tasks.ps1'
        $rehearsal=Join-Path (Split-Path $receipt) ('actual-admin-stage-rehearsal-'+$nonce)
        & $installer -Mode Stage -OutputDirectory $rehearsal @common
        & $installer -Mode Rollback -OutputDirectory $rehearsal @common
        $final=Join-Path (Split-Path $receipt) 'actual-admin-stage-final'
        & $installer -Mode Stage -OutputDirectory $final @common
        $record.status='identity_permissions_and_disabled_task_handover_verified'
        $record.task_transaction=$final;$record.authenticated_rollback=$rehearsal
        $record.tasks_after=@(Get-ScheduledTask | Where-Object TaskName -like 'StockData-*' | Select-Object TaskName,TaskPath,State)
        Save-AccessState
    } catch {$record.status='failed';$record.error=$_.Exception.Message;Save-AccessState;throw}
    finally {
        if ($record.task_created -and (Get-ScheduledTask -TaskName $probeName).State -ne 'Running') {Unregister-ScheduledTask -TaskName $probeName -Confirm:$false;$record.task_removed=$true}
        Disable-LocalUser -Name $account.Name;$record.account_enabled=$false;$credential=$null;Save-AccessState
    }
    exit 0
}
if ($Mode -eq 'PrepareAccount') {
    # Credentials stay in this local process and Windows' credential dialog.
    $credential=Get-Credential -UserName "$env:COMPUTERNAME\StockDataResearch" -Message 'Set a NEW password for the disabled StockDataResearch account. Do not enter your own Windows password.'
    if (-not $credential -or $credential.Password.Length -eq 0 -or $credential.UserName -ne "$env:COMPUTERNAME\StockDataResearch") {throw 'Cancelled or wrong account; no password reset.'}
    if ((Get-LocalUser -Name 'StockDataResearch').Enabled -or @(Get-ScheduledTask | Where-Object { $_.TaskName -like 'StockData-*' -and $_.State -ne 'Disabled' }).Count) {throw 'State changed; no password reset.'}
    $reportDir=Join-Path $repo 'reports\handover-20260917\owned-data-handover'
    $receipt=Join-Path $reportDir ('account-prepare-'+[Guid]::NewGuid().ToString('N')+'.json')
    @{status='prepared';account=$account.Name;sid=$account.SID.Value;account_enabled=$false;password_reset=$false} | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
    try {
        Set-LocalUser -Name $account.Name -Password $credential.Password
        $user=[ADSI]("WinNT://$env:COMPUTERNAME/StockDataResearch,user")
        $user.UserFlags=([int]$user.UserFlags.Value -band (-bnot 32))
        $user.SetInfo()
        @{status='password_reset_account_disabled';account=$account.Name;sid=$account.SID.Value;account_enabled=$false;password_reset=$true} | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
        & 'D:\anaconda\python.exe' -I -X utf8 -B $helper --apply --receipt ($receipt+'.batch.json')
        if ($LASTEXITCODE -ne 0) {throw 'Password reset completed but batch rights repair failed; inspect receipt.'}
        $after=Get-LocalUser -Name $account.Name
        if ($after.Enabled -or -not $after.PasswordRequired) {throw 'Account postcondition failed.'}
        @{status='account_prepared_disabled';account=$after.Name;sid=$after.SID.Value;account_enabled=$after.Enabled;password_required=$after.PasswordRequired;password_reset=$true} | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
    } finally {$credential=$null}
    exit 0
}
if (-not $task) {throw 'The batch-only Apply mode requires an existing disabled research task.'}
$answer=Read-Host 'Only grant this existing SID SeBatchLogonRight; keep account and task disabled. Type START to confirm'
if ($answer -cne 'START') { throw 'Cancelled; no security policy changes made.' }
if ((Get-LocalUser -Name 'StockDataResearch').Enabled -or (Get-ScheduledTask -TaskName 'StockData-ResearchDaily').State -ne 'Disabled') {
    throw 'State changed while awaiting confirmation.'
}
$reportDir=Join-Path $repo 'reports\repair-20260916'
$null=New-Item -ItemType Directory -Path $reportDir -Force
$receipt=Join-Path $reportDir ('batch-repair-'+[Guid]::NewGuid().ToString('N')+'.json')
& 'D:\anaconda\python.exe' -I -X utf8 -B $helper --apply --receipt $receipt
if ($LASTEXITCODE -ne 0) { throw 'Repair stopped; inspect the retained receipt. Do not enable the task or retry deployment.' }
