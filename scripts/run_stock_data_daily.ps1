param(
    [string]$Db = "kpl_data.duckdb",
    [string]$TradeDate = "",
    [ValidateSet("auto", "auction", "intraday", "close", "history", "full")]
    [string]$Phase = "auto",
    [ValidateSet("priority", "full")]
    [string]$CollectionProfile = "priority",
    [switch]$SkipCollection,
    # Keep one week of daily rollback points by default.  Backups are gzip
    # compressed after verification (the raw copy is removed), so the same
    # window costs a fraction of the previous tens-of-GB footprint.
    # Operators can override this explicitly.
    [int]$BackupKeep = 7,
    # Weekly anchors (Monday backups) kept on top of the rolling daily set.
    [int]$WeeklyKeep = 4
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "D:\anaconda\python.exe"
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$BackupDir = Join-Path $Root "backups"
$ReportDir = Join-Path $Root "reports"
$LogDir = Join-Path $Root "logs"
$IntegratedRunner = Join-Path $Root "scripts\run_integrated_daily.py"
$ObservationRunner = Join-Path $Root "scripts\audit_p0_five_day_observation.py"

$NotifyHelper = Join-Path $Root "scripts\pipeline_notify.py"

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
$backupStatus = "not_attempted"
$backupError = ""
$compressed = ""

function Compress-VerifiedBackup {
    param([string]$Path)
    # gzip the verified raw copy and remove it.  A DuckDB file is highly
    # compressible, so this cuts backup disk usage by roughly 4x.  Restore
    # with: python -c "import gzip,shutil; gzip.open(r'<file>','rb') ..."
    $gz = "$Path.gz"
    $src = [System.IO.File]::OpenRead($Path)
    $dst = [System.IO.File]::Create($gz)
    try {
        $stream = New-Object System.IO.Compression.GZipStream(
            $dst, [System.IO.Compression.CompressionLevel]::Optimal)
        try {
            $src.CopyTo($stream)
        } finally {
            $stream.Dispose()
        }
    } finally {
        $src.Dispose()
        $dst.Dispose()
    }
    Remove-Item -LiteralPath $Path -Force
    return $gz
}

function Remove-OldBackups {
    # Retention over both legacy raw copies and compressed .gz backups.
    # Newest $BackupKeep files are always kept; Monday-stamped files are
    # treated as weekly anchors and kept up to $WeeklyKeep on top of that,
    # so a bad week cannot destroy every pre-week rollback point.
    $files = @(Get-ChildItem -LiteralPath $BackupDir -File |
        Where-Object {
            $_.Name -like "kpl_data_pre_daily_*.duckdb" -or
            $_.Name -like "kpl_data_pre_daily_*.duckdb.gz"
        } |
        # Copy-Item preserves the DuckDB source timestamp, so the timestamped
        # filename is the durable creation order for retention.
        Sort-Object Name -Descending)
    if ($files.Count -le $BackupKeep) { return }
    $keptWeekly = 0
    for ($i = $BackupKeep; $i -lt $files.Count; $i++) {
        $name = $files[$i].Name
        if ($name -match "_\d{8}_") {
            $stamp = $Matches[0].Trim("_")
            try {
                if ([datetime]::ParseExact($stamp, "yyyyMMdd", $null).DayOfWeek -eq [System.DayOfWeek]::Monday) {
                    if ($keptWeekly -lt $WeeklyKeep) { $keptWeekly++; continue }
                }
            } catch { }
        }
        Remove-Item -LiteralPath $files[$i].FullName -Force
    }
}

Push-Location $Root
try {
    "DAILY_RUN_START time=$(Get-Date -Format o) phase=$Phase db=$DbPath" |
        Tee-Object -FilePath $Log -Append
    $prevEAP0 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Python $NotifyHelper --event start 2>&1 | Tee-Object -FilePath $Log -Append
    $ErrorActionPreference = $prevEAP0
    # A rollback backup is valuable, but it must not prevent the close pipeline
    # from publishing the review. Windows may reject a large Copy-Item with
    # ERROR_NOT_ENOUGH_QUOTA even when the volume has plenty of free space.
    # Record the failure and continue; the integrated runner is the source of
    # truth for the close result.
    try {
        Copy-Item -LiteralPath $DbPath -Destination $backup
        # Python writes its logging to stderr. Under $ErrorActionPreference='Stop'
        # that stderr becomes a terminating NativeCommandError even when the
        # process exits 0, so judge verification by the real process exit code.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $verifyOutput = & $Python -c "import duckdb; c=duckdb.connect(r'$backup', read_only=True); c.execute('select 1').fetchone(); c.close()" 2>&1
        $verifyCode = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        $verifyOutput | Tee-Object -FilePath $Log -Append
        if ($verifyCode -ne 0) {
            throw "Backup verification failed: $backup"
        }
        $compressed = Compress-VerifiedBackup -Path $backup
        $backupStatus = "ok"
        "BACKUP_COMPLETE path=$compressed" | Tee-Object -FilePath $Log -Append
    } catch {
        $backupStatus = "failed"
        $backupError = $_.Exception.Message
        if (Test-Path -LiteralPath $backup) {
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        }
        "BACKUP_FAILED error=$backupError; continuing close pipeline" |
            Tee-Object -FilePath $Log -Append
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
        throw "Integrated daily run failed with exit code $code. backup_status=$backupStatus"
    }
    "DAILY_RUN_COMPLETE backup_status=$backupStatus backup=$compressed" |
        Tee-Object -FilePath $Log -Append
    $prevEAP0 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Python $NotifyHelper --event success --message "close run ok; backup_status=$backupStatus; backup=$compressed" 2>&1 |
        Tee-Object -FilePath $Log -Append
    $ErrorActionPreference = $prevEAP0
} catch {
    "DAILY_RUN_FAILED time=$(Get-Date -Format o) error=$($_.Exception.Message)" |
        Tee-Object -FilePath $Log -Append
    $prevEAP0 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Python $NotifyHelper --event failure --message $_.Exception.Message 2>&1 |
        Tee-Object -FilePath $Log -Append
    $ErrorActionPreference = $prevEAP0
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
