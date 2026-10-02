param(
    [string]$Db = "kpl_data.duckdb",
    [string]$TradeDate = "",
    [ValidateSet("close")]
    [string]$Phase = "close",
    [ValidateSet("priority")]
    [string]$CollectionProfile = "priority",
    [switch]$SkipCollection,
    # Keep one week of daily rollback points by default.  Backups are gzip
    # compressed after verification (the raw copy is removed), so the same
    # window costs a fraction of the previous tens-of-GB footprint.
    # Operators can override this explicitly.
    [int]$BackupKeep = 7,
    # Weekly anchors (Monday backups) kept on top of the rolling daily set.
    [int]$WeeklyKeep = 4,
    [string]$Python = "",
    [string]$CollectorContract,
    [string]$CollectorContractSha256,
    [string]$ReportsDirectory,
    [string]$EnvironmentFile=''
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
# Missing/unbound targets are rejected before any side effects.
if (-not $CollectorContract -or -not $CollectorContractSha256 -or -not $ReportsDirectory) { throw 'Explicit collector contract, hash and reports directory required' }
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $Root '.venv\Scripts\python.exe' }
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$BackupDir = Join-Path (Split-Path -Parent $DbPath) "backups"
$ReportDir = $ReportsDirectory
$LogDir = Join-Path $ReportsDirectory "scheduled-logs"
$IntegratedRunner = Join-Path $Root "scripts\run_integrated_daily.py"
$NotifyHelper = Join-Path $Root "scripts\pipeline_notify.py"
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$backupRunId='close_'+$stamp+'_'+[Guid]::NewGuid().ToString('N')
$Log = Join-Path $LogDir ("scheduled_close_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$processState=Join-Path $LogDir ('close_'+$backupRunId+'_process.json')
$backupStatus='not_attempted'; $backupReceipt=$null; $compressed=''; $code=1
$previousDeadline=$env:STOCKDATA_PHASE_DEADLINE_EPOCH
function Write-CloseLog {
    param([Parameter(ValueFromPipeline=$true)][string]$Message)
    process {
        # New logs are UTF-8. Preserve the encoding of any legacy same-day log
        # when appending; never silently rewrite existing failure evidence.
        $encoding=[Text.UTF8Encoding]::new($false)
        if ([IO.File]::Exists($Log)) {
            $stream=[IO.File]::OpenRead($Log)
            try {if($stream.ReadByte() -eq 255 -and $stream.ReadByte() -eq 254){$encoding=[Text.Encoding]::Unicode}} finally {$stream.Dispose()}
        }
        [IO.File]::AppendAllText($Log,$Message+[Environment]::NewLine,$encoding)
        Write-Output $Message
    }
}
function Invoke-CloseNotification([string]$Event,[string]$Message='') {
    if (Test-Path -LiteralPath $NotifyHelper -PathType Leaf) {
        try {
            $notice=Invoke-StockDataProcess -Executable $Python -Arguments @($NotifyHelper,'--event',$Event,'--message',$Message) -WorkingDirectory $Root -TimeoutSeconds 15 -ProgressPath $processState
            ($notice.Stdout+$notice.Stderr) | Write-CloseLog
            if ($notice.ExitCode -ne 0) {throw 'Notification process did not complete within its cooperative budget'}
        } catch {"NOTIFICATION_DEFERRED event=$Event error=$($_.Exception.Message)" | Write-CloseLog}
    }
}
# The startup failure path must have durable evidence before DB/lock/runtime checks.
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
"DAILY_RUN_START time=$(Get-Date -Format o) phase=$Phase db=$DbPath" | Write-CloseLog
try {
    if ($EnvironmentFile) {
        if (-not [IO.Path]::IsPathRooted($EnvironmentFile) -or -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {throw 'Explicit existing absolute provider environment file required'}
        $env:KPL_ENV_FILE=$EnvironmentFile
    }
    if ($BackupKeep -lt 1 -or $WeeklyKeep -lt 0) {throw 'Positive daily and nonnegative weekly backup retention required'}
    foreach ($path in @($Python,$DbPath,$IntegratedRunner)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {throw "Required runtime/database/runner not found: $path"}
    }
    $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
    . (Join-Path $PSScriptRoot 'native_process.ps1')
    if (-not $TradeDate) {$TradeDate=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow,'China Standard Time').ToString('yyyy-MM-dd')}
    # Contract verification is read-only. Backup and collection use its same
    # original close deadline; preflight/copy may never renew the phase budget.
    $preflight=Invoke-StockDataProcess -Executable $Python -Arguments @($IntegratedRunner,'--db',$DbPath,'--reports-dir',$ReportDir,'--phase','close','--collector-contract',$CollectorContract,'--collector-contract-sha256',$CollectorContractSha256,'--dry-run') -WorkingDirectory $Root -TimeoutSeconds 60 -ProgressPath $processState
    if ($preflight.ExitCode -ne 0) {throw ('Collection contract rejected before backup: '+$preflight.Stderr)}
    $windowCode="import json,sys; from trade_system.pipeline_runtime import load_observation_contract,observation_windows; print(json.dumps(observation_windows(load_observation_contract(sys.argv[1],sys.argv[2]),sys.argv[3],'close')[0]))"
    $windowResult=Invoke-StockDataProcess -Executable $Python -Arguments @('-c',$windowCode,$CollectorContract,$CollectorContractSha256,$TradeDate) -WorkingDirectory $Root -TimeoutSeconds 30 -ProgressPath $processState
    if ($windowResult.ExitCode -ne 0) {throw ('Accepted close observation window unavailable: '+$windowResult.Stderr)}
    $window=$windowResult.Stdout | ConvertFrom-Json
    $closeDeadline=[DateTimeOffset]::Parse($window.deadline_at).ToUnixTimeSeconds()
    if ($previousDeadline) {
        $parsed=[double]::Parse($previousDeadline,[Globalization.CultureInfo]::InvariantCulture)
        if ([double]::IsNaN($parsed) -or [double]::IsInfinity($parsed)) {throw 'Finite inherited close deadline required'}
        $closeDeadline=[Math]::Min($closeDeadline,$parsed)
    }
    $env:STOCKDATA_PHASE_DEADLINE_EPOCH=$closeDeadline.ToString('R',[Globalization.CultureInfo]::InvariantCulture)
    Invoke-CloseNotification 'start'
    # Copy is guarded with PipelineLock's owner protocol. No metadata-only
    # Test-Path refusal, PID probing, deletion, or separate incompatible lock.
    # Reserve 90s before the latest start for the actual collector preflight.
    $copyDeadline=[Math]::Min($closeDeadline,[DateTimeOffset]::Parse($window.start_latest_at).ToUnixTimeSeconds()-90)
    $prepareCode="import json,sys; from trade_system.pipeline_runtime import prepare_daily_backup; print(json.dumps(prepare_daily_backup(sys.argv[1],sys.argv[2],sys.argv[3],deadline_epoch=float(sys.argv[4]))))"
    try {
        $prepared=Invoke-StockDataProcess -Executable $Python -Arguments @('-c',$prepareCode,$DbPath,$BackupDir,$backupRunId,$copyDeadline.ToString('R',[Globalization.CultureInfo]::InvariantCulture)) -WorkingDirectory $Root -DeadlineEpoch $copyDeadline -TimeoutSeconds 120 -ProgressPath $processState
        if ($prepared.ExitCode -ne 0) {throw ('Guarded raw backup failed: '+$prepared.Stderr)}
        $backupReceipt=$prepared.Stdout | ConvertFrom-Json
        $backupStatus='raw_verified_not_compressed'
        "BACKUP_RAW_VERIFIED path=$($backupReceipt.raw) qualified_recovery_point=false" | Write-CloseLog
    } catch {
        $backupStatus='failed_or_deferred'
        "BACKUP_FAILED error=$($_.Exception.Message); raw/partial retained, no retention credit; continuing collection under original deadline" | Write-CloseLog
    }
    $args = @($IntegratedRunner,"--db",$DbPath,"--reports-dir",$ReportDir,
        "--collector-contract",$CollectorContract,"--collector-contract-sha256",$CollectorContractSha256,
        "--collection-profile",$CollectionProfile,"--phase",$Phase,"--trade-date",$TradeDate)
    if ($SkipCollection) {$args+='--skip-collect'}
    Push-Location $Root
    try {
        $result=Invoke-StockDataProcess -Executable $Python -Arguments $args -WorkingDirectory $Root -DeadlineEpoch $closeDeadline -TimeoutSeconds 3600 -ProgressPath $processState
        $code=$result.ExitCode
        ($result.Stdout+$result.Stderr) | Write-CloseLog
    } finally {Pop-Location}
    # Compression is deliberately after collection. It cannot move the actual
    # close manifest outside its start window, and it keeps the verified raw if
    # its own cooperative budget or filesystem operation fails.
    if ($backupReceipt) {
        $finishCode="import json,sys; from trade_system.pipeline_runtime import finalize_daily_backup; print(json.dumps(finalize_daily_backup(sys.argv[1],deadline_epoch=float(sys.argv[2]))))"
        try {
            $finished=Invoke-StockDataProcess -Executable $Python -Arguments @('-c',$finishCode,$backupReceipt.raw,$closeDeadline.ToString('R',[Globalization.CultureInfo]::InvariantCulture)) -WorkingDirectory $Root -DeadlineEpoch $closeDeadline -TimeoutSeconds 3600 -ProgressPath $processState
            if ($finished.ExitCode -ne 0) {throw ('Archive qualification failed: '+$finished.Stderr)}
            $qualified=$finished.Stdout | ConvertFrom-Json
            if ($qualified.status -ne 'qualified' -or $qualified.qualified_recovery_point -ne $true) {throw 'Qualified recovery receipt required'}
            $compressed=$qualified.archive; $backupStatus='qualified'
            "BACKUP_COMPLETE path=$compressed qualified_recovery_point=true cleanup_errors=$($qualified.cleanup_errors -join ',')" | Write-CloseLog
        } catch {
            $backupStatus='raw_retained_unqualified_archive'
            "BACKUP_FAILED error=$($_.Exception.Message); verified raw retained; partial archive cannot consume retention quota" | Write-CloseLog
        }
    }
    if ($code -ne 0) {throw "Integrated daily run failed with exit code $code. backup_status=$backupStatus"}
    "DAILY_RUN_COMPLETE collection_only=true acceptance_verified=false backup_status=$backupStatus backup=$compressed" | Write-CloseLog
    Invoke-CloseNotification 'success' "collection run completed; acceptance_verified=false; backup_status=$backupStatus; backup=$compressed"
} catch {
    "DAILY_RUN_FAILED time=$(Get-Date -Format o) error=$($_.Exception.Message) backup_status=$backupStatus" | Write-CloseLog
    Invoke-CloseNotification 'failure' $_.Exception.Message
    throw
} finally {
    # Only qualified, hash-verified archives can be pruned. Never clean failed
    # raw/partial files as an automatic side effect of a collection failure.
    if ($compressed) {
        $retentionCode="import json,sys; from trade_system.pipeline_runtime import retain_qualified_backups; print(json.dumps(retain_qualified_backups(sys.argv[1],int(sys.argv[2]),int(sys.argv[3]),deadline_epoch=float(sys.argv[4]))))"
        try {
            $retained=Invoke-StockDataProcess -Executable $Python -Arguments @('-c',$retentionCode,$BackupDir,[string]$BackupKeep,[string]$WeeklyKeep,$closeDeadline.ToString('R',[Globalization.CultureInfo]::InvariantCulture)) -WorkingDirectory $Root -DeadlineEpoch $closeDeadline -TimeoutSeconds 120 -ProgressPath $processState
            if ($retained.ExitCode -ne 0) {throw $retained.Stderr}
            "BACKUP_RETENTION $($retained.Stdout.Trim())" | Write-CloseLog
        } catch {"BACKUP_RETENTION_DEFERRED error=$($_.Exception.Message); no unqualified recovery points pruned" | Write-CloseLog}
    }
    $env:STOCKDATA_PHASE_DEADLINE_EPOCH=$previousDeadline
}
exit $code
