[CmdletBinding()]
param([ValidateSet('Check','Apply')][string]$Mode='Check')
$ErrorActionPreference='Stop'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Use the Administrator PowerShell window. No changes made.'
}
$account=Get-LocalUser -Name 'StockDataResearch' -ErrorAction Stop
if ($account.SID.Value -ne 'S-1-5-21-3027070730-734606845-1610825463-1006' -or $account.Enabled) {
    throw 'Account differs from the reviewed disabled identity.'
}
$task=Get-ScheduledTask -TaskName 'StockData-ResearchDaily' -ErrorAction Stop
if ($task.State -ne 'Disabled' -or $task.Principal.UserId -notin @($account.Name,$account.SID.Value,"$env:COMPUTERNAME\StockDataResearch")) {
    throw 'Task differs from the reviewed disabled identity; no changes made.'
}
$repo=Split-Path -Parent $PSScriptRoot
$helper=Join-Path $repo 'tools\v2\repair_batch_logon.py'
& 'D:\anaconda\python.exe' -I -X utf8 -B $helper
if ($LASTEXITCODE -ne 0) { throw 'Read-only batch rights check failed.' }
if ($Mode -eq 'Check') { exit 0 }
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
