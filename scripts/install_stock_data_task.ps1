param(
[string]$TaskName = "StockData-DailyClose",
[string]$At = "17:30",
[string]$SupplementalAt = "20:00",
[string]$QlibAt = "20:30",
    [ValidateSet("auction", "intraday", "close")]
    [string]$Phase = "close",
    [switch]$IncludeWeekends,
    [switch]$RegisterAll,
    [switch]$Register
)

$ErrorActionPreference = "Stop"
if ($Register -or $RegisterAll) {
    throw "Legacy task registration is retired. Export existing tasks and approve a collector-only deployment first."
}
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_stock_data_daily.ps1"
$Once = Join-Path $Root "scripts\run_phase_once.ps1"
$Watch = Join-Path $Root "scripts\run_phase_watch.ps1"
$QlibRunner = Join-Path $Root "scripts\run_qlib_research_daily.py"
$SupplementalRunner = Join-Path $Root "scripts\run_supplemental_retry.ps1"
foreach ($requiredScript in @($Runner, $Once, $Watch, $QlibRunner, $SupplementalRunner)) {
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
    # The host may report a battery transition even while docked.  Windows'
    # defaults stop a long intraday watcher immediately in that case, leaving
    # LastTaskResult=0x41306 and no completion marker.  Data collection should
    # finish its bounded window regardless of AC/battery state.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours $hours) -MultipleInstances IgnoreNew
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

function Remove-LegacyPhaseTasks {
    # This task used to hold a read-only DuckDB connection for the whole day.
    # The supported intraday watcher is now run_phase_watch.ps1, which opens
    # and closes its connection per attempt under the pipeline guard.
    foreach ($legacyName in @("StockData-IntradayPush")) {
        $legacy = Get-ScheduledTask -TaskName $legacyName -ErrorAction SilentlyContinue
        if ($legacy) {
            Write-Output "REMOVE_LEGACY_TASK $legacyName"
            Unregister-ScheduledTask -TaskName $legacyName -Confirm:$false
        }
    }
}

function Register-QlibResearch([string]$startAt) {
    $argument = "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\run_qlib_research_daily.ps1`""
    $action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument $argument
    $trigger = New-PhaseTrigger $startAt
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew
    Write-Output "Task: StockData-QLibResearch"
    Write-Output ("Phase: qlib-research; schedule: {0} at {1}" -f $(if ($IncludeWeekends) { "daily" } else { "weekdays" }), $startAt)
    if ($Register) {
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName "StockData-QLibResearch" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Isolated QLib research refresh; never an execution signal" -Force | Out-Null
        $registered = Get-ScheduledTask -TaskName "StockData-QLibResearch" -ErrorAction Stop
        if ($registered.Principal.UserId -notin @("SYSTEM", "NT AUTHORITY\SYSTEM")) {
            throw "Task principal verification failed for StockData-QLibResearch`: $($registered.Principal.UserId)"
        }
        Write-Output "REGISTERED StockData-QLibResearch principal=$($registered.Principal.UserId) state=$($registered.State)"
    } else {
        Write-Output "DRY_RUN StockData-QLibResearch (pass -Register to register)"
    }
}

function Register-SupplementalRetry([string]$startAt) {
    $argument = "-NoProfile -ExecutionPolicy Bypass -File `"$SupplementalRunner`""
    $action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument $argument
    $trigger = New-PhaseTrigger $startAt
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
    Write-Output "Task: StockData-SupplementalRetry"
    Write-Output ("Phase: supplemental-retry; schedule: {0} at {1}" -f $(if ($IncludeWeekends) { "daily" } else { "weekdays" }), $startAt)
    if ($Register) {
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName "StockData-SupplementalRetry" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Retry late close supplements and republish the static review" -Force | Out-Null
        $registered = Get-ScheduledTask -TaskName "StockData-SupplementalRetry" -ErrorAction Stop
        if ($registered.Principal.UserId -notin @("SYSTEM", "NT AUTHORITY\SYSTEM")) {
            throw "Task principal verification failed for StockData-SupplementalRetry`: $($registered.Principal.UserId)"
        }
        Write-Output "REGISTERED StockData-SupplementalRetry principal=$($registered.Principal.UserId) state=$($registered.State)"
    } else {
        Write-Output "DRY_RUN StockData-SupplementalRetry (pass -Register to register)"
    }
}

if ($RegisterAll) {
    if ($Register) {
        Remove-LegacyPhaseTasks
    }
    # Auction tick data is only meaningful from 09:15.  Starting at 08:30
    # produced repeated empty KPL calls and increased block risk.  Drain at
    # 09:27 so the 09:30 intraday task cannot lose its only trigger to the
    # single-instance pipeline lock.  120s interval gives ~6 auction
    # snapshots per stock (300s produced only the first and last snap,
    # too sparse to draw the indicative-price curve).
    Register-Phase "StockData-Auction" "auction" "09:15" 120 "09:27"
    Register-Phase "StockData-Intraday" "intraday" "09:30" 300 "15:05"
    Register-Phase "StockData-DailyClose" "close" "17:30" 0 "18:30"
    Register-SupplementalRetry $SupplementalAt
    Register-QlibResearch $QlibAt
} else {
    Register-Phase $TaskName $Phase $At $(if ($Phase -eq "auction") { 120 } elseif ($Phase -eq "intraday") { 300 } else { 0 }) $(if ($Phase -eq "auction") { "09:27" } elseif ($Phase -eq "intraday") { "15:05" } else { "18:30" })
}
