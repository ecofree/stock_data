# Off-site backup sync (dry-run by default).
#
# Copies the newest daily gzip backup plus the newest weekly anchor to a
# second location: another local disk, a UNC share, or an rclone remote.
#
# Usage:
#   .\offsite_backup.ps1 -Destination D:\backups_offsite           # dry-run
#   .\offsite_backup.ps1 -Destination D:\backups_offsite -Execute
#   .\offsite_backup.ps1 -Destination "gdrive:crypto-backups" -Rclone -Execute
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [switch]$Rclone,
    [switch]$Execute,
    # Also push a copy of the current main database snapshot? Off by default:
    # the pre-daily backup is already the rollback point.
    [switch]$IncludeMainDb
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BackupDir = Join-Path $Root "backups"

if (-not (Test-Path -LiteralPath $BackupDir)) {
    throw "backup dir not found: $BackupDir"
}

$files = @()
$daily = Get-ChildItem -LiteralPath $BackupDir -File |
    Where-Object { $_.Name -like "kpl_data_pre_daily_*.duckdb.gz" } |
    Sort-Object Name -Descending | Select-Object -First 1
if ($daily) { $files += $daily }
# Weekly anchor = Monday-stamped file, newest first.
$weekly = Get-ChildItem -LiteralPath $BackupDir -File |
    Where-Object {
        $_.Name -like "kpl_data_pre_daily_*.duckdb.gz" -and
        $_.Name -match "_\d{8}_"
    } | Where-Object {
        try {
            [datetime]::ParseExact($Matches[0].Trim("_"), "yyyyMMdd", $null)
                .DayOfWeek -eq [System.DayOfWeek]::Monday
        } catch { $false }
    } | Sort-Object Name -Descending | Select-Object -First 1
if ($weekly -and ($daily -eq $null -or $weekly.FullName -ne $daily.FullName)) {
    $files += $weekly
}
if ($IncludeMainDb) {
    $mainDb = Get-ChildItem -LiteralPath $Root -Filter "kpl_data.duckdb" -File
    if ($mainDb) { $files += $mainDb }
}

if (-not $files) {
    Write-Output "nothing to sync (no backups found)"
    exit 1
}

Write-Output ("plan ({0}):" -f ($(if ($Execute) { "EXECUTE" } else { "DRY-RUN" })))
foreach ($f in $files) {
    Write-Output ("  {0}  [{1:N2} GB]" -f $f.Name, ($f.Length / 1GB))
}

if (-not $Execute) {
    Write-Output "dry-run only; pass -Execute to copy"
    exit 0
}

foreach ($f in $files) {
    if ($Rclone) {
        & rclone copyto $f.FullName ("{0}/{1}" -f $Destination.TrimEnd('/'), $f.Name)
        if ($LASTEXITCODE -ne 0) { throw "rclone failed for $($f.Name)" }
    } else {
        if (-not (Test-Path -LiteralPath $Destination)) {
            New-Item -ItemType Directory -Path $Destination -Force | Out-Null
        }
        Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $Destination $f.Name) -Force
    }
    Write-Output "copied: $($f.Name)"
}
Write-Output "offsite backup complete"
