[CmdletBinding()]
param([switch]$Build, [switch]$Update, [switch]$NoBrowser, [switch]$Background, [int]$Port=8769,
      [string]$Python='D:\anaconda\python.exe')
$ErrorActionPreference='Stop'
$projectPath=Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python interpreter not found' }
Set-Location -LiteralPath $projectPath
if (($Build -or $Update) -and -not $env:KPL_ENV_FILE) { $env:KPL_ENV_FILE='D:\accio\stock_data\.env' }
if ($Build) {
    & $Python -m trade_system.v2.research_product build
    if ($LASTEXITCODE -ne 0) { throw 'Research build failed; prior successful version is retained.' }
}
if (-not (Test-Path -LiteralPath 'reports/research-delivery/research-current.json')) {
    throw 'No frozen research build. Viewing never starts training automatically; use -Build explicitly after preparing inputs.'
}
if ($Update) {
    & $Python -m trade_system.v2.research_product update
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Current update failed. The prior version remains available with its original date.' }
}
$browserArgs=@()
if (-not $NoBrowser) { $browserArgs=@('--open-browser') }
$expectedText=& $Python -c 'import json,sys; from trade_system.v2.research_product_server import service_identity; print(json.dumps(service_identity(sys.argv[1],sys.argv[2])))' $projectPath (Join-Path $projectPath 'reports\research-delivery')
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify current workspace source.' }
$expected=$expectedText | ConvertFrom-Json
function Test-WorkspaceIdentity($actual) {
    return $actual -and $actual.protocol -eq $expected.protocol -and $actual.workspace_id -eq $expected.workspace_id -and $actual.surface_sha256 -eq $expected.surface_sha256 -and $actual.source_matches -eq $true
}
$existing=$null
try { $existing=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 } catch { }
if (Test-WorkspaceIdentity $existing) {
    $null=Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 15
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/" }
    Write-Output "Research desk is already running: http://127.0.0.1:$Port/"
    exit 0
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port occupied by another/old service. Do not reuse it or kill its owner. Stop the known workspace normally or select a free -Port.'
}
if ($Background) {
    $logPath=Join-Path $projectPath 'reports\research-delivery\service-logs'
    $null=New-Item -ItemType Directory -Path $logPath -Force
    $runTag=Get-Date -Format 'yyyyMMdd_HHmmss_fff'
    $serviceProcess=Start-Process -FilePath $Python -ArgumentList @('-m','trade_system.v2.research_product','serve','--port',"$Port") -WorkingDirectory $projectPath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logPath "$runTag.out.log") -RedirectStandardError (Join-Path $logPath "$runTag.err.log")
    $ready=$false
    for ($attempt=0; $attempt -lt 40; $attempt++) {
        if ($serviceProcess.HasExited) { throw "Workspace failed to start. Inspect $logPath\$runTag.err.log" }
        try {
            $health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if (Test-WorkspaceIdentity $health) {
                $null=Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 15
                $ready=$true;break
            }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'Workspace did not become healthy. Process retained for diagnosis; no force stop was used.' }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/" }
    Write-Output "Current workspace ready: http://127.0.0.1:$Port/ (viewing did not fetch data or train)"
    exit 0
}
& $Python -m trade_system.v2.research_product serve --port $Port @browserArgs
exit $LASTEXITCODE
