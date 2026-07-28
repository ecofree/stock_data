param(
    [string]$TaskName = "StockData-DailyClose",
    [string]$At = "17:30",
    [ValidateSet("auction", "intraday", "close")]
    [string]$Phase = "close",
    [switch]$IncludeWeekends,
    [switch]$RegisterAll,
    [switch]$Register
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_stock_data_daily.ps1"
$Once = Join-Path $Root "scripts\run_phase_once.ps1"
$Watch = Join-Path $Root "scripts\run_phase_watch.ps1"
foreach ($requiredScript in @($Runner, $Once, $Watch)) {
    if (-not (Test-Path -LiteralPath $requiredScript)) {
        throw "Scheduler script not found: $requiredScript"
    }
}
if ($Register) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        throw (
            "SYSTEM task registration requires elevated PowerShell. Re-run as Administrator: " +
            "PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
            "-RegisterAll -Register"
        )
    }
}

$days = if ($IncludeWeekends) { $null } else { @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday") }

function New-PhaseTrigger([string]$timeText) {
    if ($IncludeWeekends) {
        return New-ScheduledTaskTrigger -Daily -At $timeText
    }
    return New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days -At $timeText
}

function Register-Phase([string]$name, [string]$phaseName, [string]$startAt, [int]$interval, [string]$endAt) {
    $isWatch = $phaseName -in @("auction", "intraday")
    if ($isWatch) {
        $lunchArg = if ($phaseName -eq "intraday") { " -SkipLunch" } else { "" }
        $argument = "-NoProfile -ExecutionPolicy Bypass -File `"$Watch`" -Phase $phaseName -IntervalSeconds $interval -EndAt $endAt$lunchArg -ContinueOnFailure"
        $action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument $argument
        $hours = 8
    } else {
        $action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument (
            "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Phase close -CollectionProfile priority"
        )
        $hours = 4
    }
    $trigger = New-PhaseTrigger $startAt
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours $hours) -MultipleInstances IgnoreNew
    Write-Output "Task: $name"
    Write-Output ("Phase: {0}; schedule: {1} at {2}" -f $phaseName, $(if ($IncludeWeekends) { "daily" } else { "weekdays" }), $startAt)
    Write-Output ("Action: {0}" -f $(if ($isWatch) { $Watch } else { $Runner }))
    if ($Register) {
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Stock data $phaseName phase with single-instance guard and durable audit" -Force | Out-Null
        $registered = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        if ($registered.Principal.UserId -notin @("SYSTEM", "NT AUTHORITY\SYSTEM")) {
            throw "Task principal verification failed for $name`: $($registered.Principal.UserId)"
        }
        Write-Output "REGISTERED $name principal=$($registered.Principal.UserId) state=$($registered.State)"
    } else {
        Write-Output "DRY_RUN $name (pass -Register to register)"
    }
}

if ($RegisterAll) {
    # Auction tick data is only meaningful from 09:15.  Starting at 08:30
    # produced repeated empty KPL calls and increased block risk.  Drain at
    # 09:27 so the 09:30 intraday task cannot lose its only trigger to the
    # single-instance pipeline lock.
    Register-Phase "StockData-Auction" "auction" "09:15" 300 "09:27"
    Register-Phase "StockData-Intraday" "intraday" "09:30" 300 "15:05"
    Register-Phase "StockData-DailyClose" "close" "17:30" 0 "18:30"
} else {
    Register-Phase $TaskName $Phase $At $(if ($Phase -eq "auction") { 300 } elseif ($Phase -eq "intraday") { 300 } else { 0 }) $(if ($Phase -eq "auction") { "09:27" } elseif ($Phase -eq "intraday") { "15:05" } else { "18:30" })
}
