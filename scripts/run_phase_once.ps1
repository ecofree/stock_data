param(
    [string]$Db = "kpl_data.duckdb",
    [ValidateSet("auction", "intraday", "close")]
    [string]$Phase = "close",
    [string]$TradeDate = ""
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "D:\anaconda\python.exe"
$IntegratedRunner = Join-Path $Root "scripts\run_integrated_daily.py"
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}
if (-not (Test-Path -LiteralPath $IntegratedRunner)) {
    throw "Integrated runner not found: $IntegratedRunner"
}
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$Log = Join-Path $LogDir ("scheduled_{0}_{1}.log" -f $Phase, (Get-Date -Format "yyyy-MM-dd"))
$args = @(
    $IntegratedRunner,
    "--db", $DbPath,
    "--reports-dir", (Join-Path $Root "reports"),
    "--phase", $Phase,
    "--collection-profile", "priority"
)
if ($TradeDate) { $args += @("--trade-date", $TradeDate) }

Push-Location $Root
try {
    $output = & $Python @args 2>&1
    $output | Tee-Object -FilePath $Log -Append
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($null -eq $code) { $code = 1 }
Write-Output "PHASE_RUN phase=$Phase code=$code log=$Log"
exit $code
