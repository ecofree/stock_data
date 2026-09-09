param(
    [ValidateSet("auction", "intraday")]
    [string]$Phase = "intraday",
    [int]$IntervalSeconds = 300,
    [string]$EndAt = "15:05",
    [int]$MinRunWindowSeconds = 0,
    [string]$Db = "kpl_data.duckdb",
    [switch]$SkipLunch,
    [switch]$ContinueOnFailure
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Once = Join-Path $Root "scripts\run_phase_once.ps1"
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$LogDir = Join-Path $Root "logs"
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
    # A complete auction collection can take several minutes. Do not start
    # another run when the 09:27 drain leaves only a short tail.
    360
} else {
    # Intraday also needs a bounded-run window before the 15:05 drain.
    600
}
$attempts = 0
$successes = 0
$failures = 0
$deferred = 0
$nextRun = Get-Date

function Write-WatchEvent([string]$Message) {
    $line = "$(Get-Date -Format o) $Message"
    $line | Tee-Object -FilePath $Log -Append
}

Write-WatchEvent "PHASE_WATCH_START phase=$Phase db=$DbPath interval=$IntervalSeconds end=$EndAt"
while ((Get-Date) -lt $endAtToday) {
    $remainingBeforeRun = [int][Math]::Max(0, ($endAtToday - (Get-Date)).TotalSeconds)
    if ($remainingBeforeRun -lt [Math]::Max(30, $effectiveMinRunWindow)) {
        Write-WatchEvent "PHASE_WATCH_DRAIN phase=$Phase remaining=$remainingBeforeRun min_run_window=$effectiveMinRunWindow"
        break
    }
    if ($SkipLunch -and $Phase -eq "intraday" -and (Get-Date).TimeOfDay -ge ([timespan]::Parse("11:30")) -and (Get-Date).TimeOfDay -lt ([timespan]::Parse("13:00"))) {
        $sleepUntil = (Get-Date).Date.AddHours(13)
        Start-Sleep -Seconds ([int][Math]::Max(1, ($sleepUntil - (Get-Date)).TotalSeconds))
        $nextRun = Get-Date
        continue
    }
    $attempts++
    Write-WatchEvent "PHASE_WATCH_ATTEMPT phase=$Phase attempt=$attempts"
    & PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $Once -Db $DbPath -Phase $Phase
    $runCode = $LASTEXITCODE
    if ($runCode -eq 0) {
        $successes++
    } elseif ($runCode -eq 3) {
        # Another phase owns the single-writer pipeline lock.  This attempt
        # was safely deferred; counting it as a provider failure makes the
        # scheduler red even though no data task actually failed.
        $deferred++
        Write-WatchEvent "PHASE_WATCH_DEFER phase=$Phase attempt=$attempts reason=pipeline_busy"
    } else {
        $failures++
        if (-not $ContinueOnFailure) {
            Write-WatchEvent "PHASE_WATCH_STOP phase=$Phase reason=run_failed code=$runCode"
            break
        }
    }
    $remaining = [int][Math]::Max(0, ($endAtToday - (Get-Date)).TotalSeconds)
    if ($remaining -le 0) { break }
    # Keep the interval anchored to the scheduled start time.  Sleeping for
    # interval seconds after a long run silently turns a 5-minute watcher
    # into a 9-10 minute cadence and creates false freshness gaps.
    $nextRun = $nextRun.AddSeconds([Math]::Max(30, $IntervalSeconds))
    $sleepSeconds = ($nextRun - (Get-Date)).TotalSeconds
    if ($sleepSeconds -gt 0) {
        Start-Sleep -Seconds ([int][Math]::Min($sleepSeconds, $remaining))
    } else {
        Write-WatchEvent "PHASE_WATCH_OVERRUN phase=$Phase lag_seconds=$([int][Math]::Abs($sleepSeconds))"
        $nextRun = Get-Date
    }
}

Write-WatchEvent "PHASE_WATCH_COMPLETE phase=$Phase attempts=$attempts successes=$successes deferred=$deferred failures=$failures end=$EndAt"
# A watch is successful only when every attempted run succeeded.  A single
# green attempt must not hide later provider failures or stale data.
if ($attempts -gt 0 -and $failures -eq 0) { exit 0 }
exit 1
