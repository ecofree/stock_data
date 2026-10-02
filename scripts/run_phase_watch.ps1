param(
    [ValidateSet("auction", "intraday")]
    [string]$Phase = "intraday",
    [int]$IntervalSeconds = 300,
    [string]$EndAt = "15:05",
    [int]$MinRunWindowSeconds = 0,
    [string]$Db = "kpl_data.duckdb",
    [switch]$SkipLunch,
    [switch]$ContinueOnFailure,
    [string]$Python = "",
    [string]$CollectorContract,
    [string]$CollectorContractSha256,
    [string]$ReportsDirectory,
    [string]$EnvironmentFile=''
)

$ErrorActionPreference = "Continue"
# Keep -Db usable; advanced parameters reserve that alias for -Debug.
if (-not $CollectorContract -or -not $CollectorContractSha256 -or -not $ReportsDirectory) { throw 'Explicit collector contract, hash and reports directory required' }
if ($EnvironmentFile) {
    if (-not [IO.Path]::IsPathRooted($EnvironmentFile) -or -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {throw 'Explicit existing absolute provider environment file required'}
    $env:KPL_ENV_FILE=$EnvironmentFile
}
$Root = Split-Path -Parent $PSScriptRoot
$Once = Join-Path $Root "scripts\run_phase_once.ps1"
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$LogDir = Join-Path $ReportsDirectory "scheduled-logs"
if (-not (Test-Path -LiteralPath $Once)) {
    throw "Phase runner not found: $Once"
}
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$Log = Join-Path $LogDir ("scheduled_{0}_watch_{1}.log" -f $Phase, (Get-Date -Format "yyyy-MM-dd"))
$endTime = [datetime]::ParseExact($EndAt, "HH:mm", $null)
$endAtToday = (Get-Date).Date.Add($endTime.TimeOfDay)
$effectiveMinRunWindow = if ($MinRunWindowSeconds -gt 0) {
    $MinRunWindowSeconds
} elseif ($Phase -eq "auction") {
    # The scoped tick request has a 45-second network budget. Reserve 90s
    # for it and the local projection; a 360s start margin stopped at 09:21
    # and prevented the essential 09:25 observation. This is a start margin,
    # never permission to kill a writer at the end of the watch window.
    90
} else {
    # Intraday also needs a bounded-run window before the 15:05 drain.
    270
}
function Write-WatchEvent([string]$Message) {
    $line = "$(Get-Date -Format o) $Message"
    $line | Tee-Object -FilePath $Log -Append
}

function Get-WatchClock {
    [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, 'China Standard Time')
}

function Wait-WatchUntil([DateTimeOffset]$Target) {
    while ((Get-WatchClock) -lt $Target) {
        Start-Sleep -Seconds ([int][Math]::Max(1, [Math]::Min(60, ($Target - (Get-WatchClock)).TotalSeconds)))
    }
}

function Invoke-WatchAttempt([DateTimeOffset]$Deadline, [string]$WindowId, [bool]$Prepare) {
    $previousDeadline = $env:STOCKDATA_PHASE_DEADLINE_EPOCH
    $previousWindowDeadline = $env:STOCKDATA_OBSERVATION_WINDOW_DEADLINE_EPOCH
    $previousWindowId = $env:STOCKDATA_OBSERVATION_WINDOW_ID
    $env:STOCKDATA_PHASE_DEADLINE_EPOCH = $Deadline.ToUnixTimeSeconds().ToString()
    $env:STOCKDATA_OBSERVATION_WINDOW_DEADLINE_EPOCH = $Deadline.ToUnixTimeSeconds().ToString()
    $env:STOCKDATA_OBSERVATION_WINDOW_ID = $WindowId
    try {
        $onceArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Once, '-Db', $DbPath,
            '-Phase', $Phase, '-Python', $Python, '-CollectorContract', $CollectorContract,
            '-CollectorContractSha256', $CollectorContractSha256, '-ReportsDirectory', $ReportsDirectory)
        if ($EnvironmentFile) { $onceArgs += @('-EnvironmentFile', $EnvironmentFile) }
        if ($Prepare) { $onceArgs += '-PrepareReference' }
        & PowerShell.exe @onceArgs
        $script:runCode = $LASTEXITCODE
    } finally {
        $env:STOCKDATA_PHASE_DEADLINE_EPOCH = $previousDeadline
        $env:STOCKDATA_OBSERVATION_WINDOW_DEADLINE_EPOCH = $previousWindowDeadline
        $env:STOCKDATA_OBSERVATION_WINDOW_ID = $previousWindowId
    }
}

if (-not $Python) { $Python = Join-Path $Root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python runtime not found: $Python" }
$day = (Get-WatchClock).ToString('yyyy-MM-dd')
# The same pure expansion is used by complete-day acceptance. There is no
# fallback policy for old contracts and no network/calendar repair in a watch.
$readPolicy = "import json,sys; from trade_system.pipeline_runtime import load_observation_contract,observation_windows; from trade_system.trading_calendar import trading_session_status; p=load_observation_contract(sys.argv[1],sys.argv[2]); s=trading_session_status(sys.argv[5],sys.argv[3]); print(json.dumps({'state':s.state,'reason':s.reason,'windows':observation_windows(p,sys.argv[3],sys.argv[4])}))"
Push-Location $Root
try {
    $policyOutput = & $Python -c $readPolicy $CollectorContract $CollectorContractSha256 $day $Phase $DbPath
    if ($LASTEXITCODE -ne 0) { throw 'Accepted observation window contract is unavailable; no collection started' }
    $schedule = ($policyOutput -join "`n") | ConvertFrom-Json
} finally { Pop-Location }
Write-WatchEvent "PHASE_WATCH_START phase=$Phase db=$DbPath interval=$IntervalSeconds end=$EndAt contract=$CollectorContractSha256"
if ($schedule.state -eq 'closed') {
    Write-WatchEvent "PHASE_WATCH_MARKET_CLOSED phase=$Phase day=$day no_observation_windows_required=true"
    exit 0
}
if ($schedule.state -ne 'open') {
    Write-WatchEvent "PHASE_WATCH_STOP phase=$Phase reason=calendar_unverified detail=$($schedule.reason)"
    exit 2
}
$windows = @($schedule.windows)
$endOffset = [DateTimeOffset]::ParseExact(($day+'T'+$EndAt+':00+08:00'), 'yyyy-MM-ddTHH:mm:sszzz', $null)
foreach ($window in $windows) {
    if ($window.interval_seconds -ne $IntervalSeconds -or
        [DateTimeOffset]::Parse($window.deadline_at) -gt $endOffset.AddSeconds(-15)) {
        throw 'Watch interval/end differs from accepted observation contract; no collection started'
    }
}
$prepareAttempts = 0
$prepareFailures = 0
# Early reference preparation has its own scope and is never an auction pass.
if ($Phase -eq 'auction') {
    $prepareEnd = [DateTimeOffset]::Parse($day+'T09:15:00+08:00')
    $nextPreparation = Get-WatchClock
    while ((Get-WatchClock) -lt $prepareEnd) {
        $remaining = ($prepareEnd - (Get-WatchClock)).TotalSeconds
        if ($remaining -lt [Math]::Max(30, $effectiveMinRunWindow)) {
            Write-WatchEvent "PHASE_WATCH_DRAIN phase=$Phase scope=prepare_reference remaining=$remaining"
            break
        }
        $prepareAttempts++
        Invoke-WatchAttempt $prepareEnd '' $true
        if ($runCode -notin @(0,3)) { $prepareFailures++ }
        Write-WatchEvent "PHASE_WATCH_PREPARE phase=$Phase attempt=$prepareAttempts code=$runCode core_window_pass=false"
        $nextPreparation = $nextPreparation.AddSeconds($IntervalSeconds)
        while ($nextPreparation -le (Get-WatchClock)) { $nextPreparation = $nextPreparation.AddSeconds($IntervalSeconds) }
        if ($nextPreparation -lt $prepareEnd) { Wait-WatchUntil $nextPreparation } else { break }
    }
}
$attempts = 0
$successes = 0
$failures = 0
$deferred = 0
$missed = 0
foreach ($window in $windows) {
    $start = [DateTimeOffset]::Parse($window.start_at)
    $latestStart = [DateTimeOffset]::Parse($window.start_latest_at)
    $deadline = [DateTimeOffset]::Parse($window.deadline_at)
    Wait-WatchUntil $start
    if (($endOffset - (Get-WatchClock)).TotalSeconds -lt [Math]::Max(30, $effectiveMinRunWindow)) {
        $missed++
        Write-WatchEvent "PHASE_WATCH_DRAIN phase=$Phase window=$($window.window_id) reason=min_run_window_unavailable"
        continue
    }
    if ((Get-WatchClock) -gt $latestStart) {
        $missed++
        Write-WatchEvent "PHASE_WATCH_MISSED_WINDOW phase=$Phase window=$($window.window_id) reason=start_window_elapsed"
        continue
    }
    $attempts++
    Write-WatchEvent "PHASE_WATCH_ATTEMPT phase=$Phase window=$($window.window_id) deadline=$($deadline.ToString('o')) attempt=$attempts"
    # Exactly one automatic attempt per existing slot, never a renewed request
    # budget. The Python runner cooperatively caps work to this original deadline.
    Invoke-WatchAttempt $deadline $window.window_id $false
    if ($runCode -eq 0 -and (Get-WatchClock) -le $deadline) {
        $successes++
    } elseif ($runCode -eq 3) {
        $deferred++
        Write-WatchEvent "PHASE_WATCH_DEFER phase=$Phase window=$($window.window_id) reason=pipeline_busy required_window_unqualified=true"
    } else {
        $failures++
        if (-not $ContinueOnFailure) {
            Write-WatchEvent "PHASE_WATCH_STOP phase=$Phase reason=run_failed code=$runCode"
            break
        }
    }
}
$unattempted = $windows.Count - $attempts - $missed
# Let the final original budget close before classifying it. This wait starts
# no new work and permits only an already authorized bounded recovery receipt.
if ($unattempted -eq 0) { Wait-WatchUntil ([DateTimeOffset]::Parse($windows[-1].deadline_at)) }
$readResult = "import json,sys; from trade_system.pipeline_runtime import load_observation_contract,all_manifests; from trade_system.p0_observation import audit_phase_windows; p=load_observation_contract(sys.argv[1],sys.argv[2]); r=audit_phase_windows(all_manifests(sys.argv[5]),sys.argv[3],sys.argv[4],sys.argv[2],p); print(json.dumps({'passed':r['passed'],'required':r['required_window_count'],'qualified':r['qualified_window_count'],'errors':r['evidence_errors']}))"
Push-Location $Root
try {
    $resultOutput = & $Python -c $readResult $CollectorContract $CollectorContractSha256 $day $Phase $ReportsDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Observation receipts could not be verified; watch remains unqualified' }
    $windowResult = ($resultOutput -join "`n") | ConvertFrom-Json
} finally { Pop-Location }
Write-WatchEvent "PHASE_WATCH_COMPLETE phase=$Phase required_windows=$($windows.Count) attempts=$attempts successes=$successes deferred=$deferred failures=$failures missed=$missed unattempted=$unattempted prepare_attempts=$prepareAttempts prepare_failures=$prepareFailures end=$EndAt"
Write-WatchEvent "PHASE_WATCH_QUALIFICATION phase=$Phase qualified_windows=$($windowResult.qualified)/$($windowResult.required) passed=$($windowResult.passed) errors=$($windowResult.errors -join ',')"
# Closed sessions and lunch have no declared slots. On an open day, every
# declared slot must qualify; a green latest receipt cannot hide another slot.
if ($windowResult.passed -eq $true) { exit 0 }
exit 1
