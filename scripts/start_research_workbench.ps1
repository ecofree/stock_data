[CmdletBinding()]
param([switch]$Build, [switch]$Update, [switch]$NoBrowser, [int]$Port=8766,
      [string]$Python='D:\anaconda\python.exe')
$ErrorActionPreference='Stop'
$projectPath=Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python interpreter not found' }
Set-Location -LiteralPath $projectPath
if (-not $env:KPL_ENV_FILE) { $env:KPL_ENV_FILE='D:\accio\stock_data\.env' }
if ($Build -or -not (Test-Path -LiteralPath 'reports/research-delivery/research-current.json')) {
    & $Python tools/v2/research_workbench.py build
    if ($LASTEXITCODE -ne 0) { throw 'Research build failed; prior successful version is retained.' }
}
if ($Update) {
    & $Python tools/v2/research_workbench.py update
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Current update failed. The prior version remains available with its original date.' }
}
$browserArgs=@()
if (-not $NoBrowser) { $browserArgs=@('--open-browser') }
$existing=$null
try { $existing=Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2 } catch { }
if ($existing -and $existing.Headers['X-Stock-Research'] -eq 'local-v1') {
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/" }
    Write-Output "Research desk is already running: http://127.0.0.1:$Port/"
    exit 0
}
& $Python tools/v2/research_workbench.py serve --port $Port @browserArgs
exit $LASTEXITCODE
