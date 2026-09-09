param(
    [string]$TradeDate = "",
    [string]$Db = "",
    [string]$ReportsDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "D:\anaconda\python.exe"
$Runner = Join-Path $Root "scripts\run_supplemental_retry.py"
if (-not (Test-Path -LiteralPath $Python)) { throw "Python runtime not found: $Python" }
if (-not (Test-Path -LiteralPath $Runner)) { throw "Retry runner not found: $Runner" }
if (-not $Db) { $Db = Join-Path $Root "kpl_data.duckdb" }
if (-not $ReportsDir) { $ReportsDir = Join-Path $Root "reports" }

$arguments = @($Runner, "--db", $Db, "--reports-dir", $ReportsDir)
if ($TradeDate) { $arguments += @("--trade-date", $TradeDate) }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
& $Python @arguments
exit $LASTEXITCODE
