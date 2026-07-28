param(
    [string]$Db = "kpl_data.duckdb",
    [string]$TradeDate = "",
    [ValidateSet("auto", "auction", "intraday", "close", "history", "full")]
    [string]$Phase = "auto",
    [ValidateSet("priority", "full")]
    [string]$CollectionProfile = "priority",
    [switch]$SkipCollection,
    [int]$BackupKeep = 14
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "D:\anaconda\python.exe"
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$BackupDir = Join-Path $Root "backups"
$ReportDir = Join-Path $Root "reports"
$LogDir = Join-Path $Root "logs"
$IntegratedRunner = Join-Path $Root "scripts\run_integrated_daily.py"
$ObservationRunner = Join-Path $Root "scripts\audit_p0_five_day_observation.py"

if (-not (Test-Path -LiteralPath $DbPath)) {
    throw "Database not found: $DbPath"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}
if (-not (Test-Path -LiteralPath $IntegratedRunner)) {
    throw "Integrated runner not found: $IntegratedRunner"
}
if (-not (Test-Path -LiteralPath $ObservationRunner)) {
    throw "P0 observation runner not found: $ObservationRunner"
}
if (Test-Path -LiteralPath "$DbPath.pipeline.lock") {
    throw "Refusing to copy a database while the pipeline lock exists: $DbPath.pipeline.lock"
}
if (-not (Test-Path -LiteralPath $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir | Out-Null
}
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = Join-Path $BackupDir ("kpl_data_pre_daily_{0}.duckdb" -f $stamp)
$Log = Join-Path $LogDir ("scheduled_close_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

function Remove-OldBackups {
    $old = Get-ChildItem -LiteralPath $BackupDir -Filter "kpl_data_pre_daily_*.duckdb" -File |
        # Copy-Item preserves the DuckDB source timestamp, so the timestamped
        # filename is the durable creation order for retention.
        Sort-Object Name -Descending | Select-Object -Skip ([Math]::Max(1, $BackupKeep))
    foreach ($file in $old) {
        Remove-Item -LiteralPath $file.FullName -Force
    }
}

Push-Location $Root
try {
    "DAILY_RUN_START time=$(Get-Date -Format o) phase=$Phase db=$DbPath" |
        Tee-Object -FilePath $Log -Append
    Copy-Item -LiteralPath $DbPath -Destination $backup
    # Python writes its logging to stderr.  Under $ErrorActionPreference='Stop' that
    # stderr is turned into a terminating NativeCommandError even when the process
    # exits 0, which previously killed the whole close run.  Scope the native call to
    # 'Continue' and judge success by the real process exit code instead.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $verifyOutput = & $Python -c "import duckdb; c=duckdb.connect(r'$backup', read_only=True); c.execute('select 1').fetchone(); c.close()" 2>&1
    $verifyCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    $verifyOutput | Tee-Object -FilePath $Log -Append
    if ($verifyCode -ne 0) {
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        throw "Backup verification failed: $backup"
    }

    $args = @(
        $IntegratedRunner,
        "--db", $DbPath,
        "--reports-dir", $ReportDir,
        "--collection-profile", $CollectionProfile,
        "--phase", $Phase
    )
    if ($TradeDate) { $args += @("--trade-date", $TradeDate) }
    if ($SkipCollection) { $args += "--skip-collect" }

    # Same reasoning as the backup-verify call: scope to 'Continue' so Python's stderr
    # logging does not abort the close run; success is judged by $code below.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & $Python @args 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    $output | Tee-Object -FilePath $Log -Append
    if ($code -ne 0) {
        throw "Integrated daily run failed with exit code $code. Backup: $backup"
    }
    "DAILY_RUN_COMPLETE backup=$backup" | Tee-Object -FilePath $Log -Append
} catch {
    "DAILY_RUN_FAILED time=$(Get-Date -Format o) error=$($_.Exception.Message)" |
        Tee-Object -FilePath $Log -Append
    throw
} finally {
    $observationDate = if ($TradeDate) { $TradeDate } else { Get-Date -Format "yyyy-MM-dd" }
    try {
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $observationOutput = & $Python $ObservationRunner `
            --db $DbPath `
            --as-of $observationDate `
            --reports-dir $ReportDir `
            --out (Join-Path $ReportDir "p0_five_day_observation_latest.md") 2>&1
        $observationCode = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        $observationOutput | Tee-Object -FilePath $Log -Append
        if ($observationCode -ne 0) {
            "P0_OBSERVATION_FAILED code=$observationCode" | Tee-Object -FilePath $Log -Append
        }
    } catch {
        "P0_OBSERVATION_FAILED error=$($_.Exception.Message)" | Tee-Object -FilePath $Log -Append
    }
    Remove-OldBackups
    Pop-Location
}
