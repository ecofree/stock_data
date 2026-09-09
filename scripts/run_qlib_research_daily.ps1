param(
    [string]$Db = "kpl_data.duckdb",
    [string]$TradeDate = ""
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv-qlib\Scripts\python.exe"
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$Runner = Join-Path $Root "scripts\run_qlib_research_daily.py"
$Reports = Join-Path $Root "reports"

foreach ($required in @($Python, $Runner, $DbPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "QLib research dependency not found: $required"
    }
}

$args = @($Runner, "--db", $DbPath, "--reports-dir", $Reports)
if ($TradeDate) { $args += @("--trade-date", $TradeDate) }
& $Python @args
exit $LASTEXITCODE
