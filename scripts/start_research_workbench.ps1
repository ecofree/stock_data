[CmdletBinding()]
param([switch]$Build, [switch]$Update, [switch]$NoBrowser, [switch]$Background, [int]$Port=8769,
      [string]$Python='', [string]$Workspace='', [string]$ReleaseDirectory='',
      [string]$ReleaseManifestSha256='', [string]$RuntimeRoot='', [string]$DeploymentState='')
$ErrorActionPreference='Stop'
if ($Build -or $Update) { throw 'Retired model build/update; this entry only opens the released read-only workspace' }
$projectPath=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native_process.ps1')
if (-not $DeploymentState -and $RuntimeRoot -and -not $ReleaseDirectory) {
    $DeploymentState=Join-Path $RuntimeRoot 'state\current.json'
}
if ($DeploymentState) {
    if ($Python -or $Workspace -or $ReleaseDirectory -or $ReleaseManifestSha256) {throw 'Use one deployed state or explicit release fields, not a mixture'}
    if (-not [IO.Path]::IsPathRooted($DeploymentState) -or -not (Test-Path -LiteralPath $DeploymentState -PathType Leaf)) {throw 'Explicit current deployed state is missing; no checkout fallback'}
    if ((Get-Item -LiteralPath $DeploymentState).Length -gt 100000) {throw 'Deployment state exceeds read budget'}
    $state=[IO.File]::ReadAllText($DeploymentState) | ConvertFrom-Json
    if ($state.schema -ne 'stockdata_runtime_state_v1' -or $state.status -ne 'active' -or $state.production_cutover -ne $true) {throw 'Current state is not an accepted production handover'}
    if ($RuntimeRoot -and [IO.Path]::GetFullPath($RuntimeRoot) -ne [IO.Path]::GetFullPath($state.runtime_root)) {throw 'Runtime root differs from current deployment'}
    $RuntimeRoot=[string]$state.runtime_root
    $Python=[string]$state.publication.python
    $Workspace=[string]$state.publication.workspace
    $ReleaseDirectory=[string]$state.publication.release_directory
    $ReleaseManifestSha256=[string]$state.publication.manifest_sha256
}
foreach ($path in @($Python,$Workspace,$ReleaseDirectory,$RuntimeRoot)) {
    if (-not $path -or -not [IO.Path]::IsPathRooted($path)) {throw 'Explicit released interpreter, workspace, release and runtime root required; no .venv or old-directory fallback'}
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {throw 'Dedicated released Python is missing'}
if ($ReleaseManifestSha256 -notmatch '^[a-fA-F0-9]{64}$') {throw 'Approved release manifest SHA256 required'}
$Workspace=(Resolve-Path -LiteralPath $Workspace).Path
$ReleaseDirectory=(Resolve-Path -LiteralPath $ReleaseDirectory).Path
if ($Workspace.Equals($ReleaseDirectory,[StringComparison]::OrdinalIgnoreCase) -or
    $Workspace.StartsWith($ReleaseDirectory.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Workspace must be outside immutable release'}
if (-not (Test-Path -LiteralPath (Join-Path $Workspace 'workspace-config.json') -PathType Leaf)) {throw 'Existing read-only workspace configuration required'}
$manifest=Join-Path $ReleaseDirectory 'research-release.json'
if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash -ne $ReleaseManifestSha256) {throw 'Release manifest differs from the accepted binding'}
$launcher=Join-Path $ReleaseDirectory 'run_research.py'
Set-StockDataRuntimeRoot $RuntimeRoot
$env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8'
$dependency=Invoke-StockDataProcess -Executable $Python -Arguments @('-I','-B','-m','pip','check') -WorkingDirectory $ReleaseDirectory -TimeoutSeconds 60
if ($dependency.ExitCode -ne 0) {throw 'Runtime dependency check failed; no build, update or service was started.'}
$prefix=@('-I','-X','utf8','-B',$launcher)
$identityResult=Invoke-StockDataProcess -Executable $Python -Arguments ($prefix+@('identity','--output',$Workspace)) -WorkingDirectory $ReleaseDirectory -TimeoutSeconds 30
if ($identityResult.ExitCode -ne 0) {throw 'Cannot identify the sealed workspace source'}
$expected=$identityResult.Stdout | ConvertFrom-Json
function Test-WorkspaceIdentity($actual) {
    return $actual -and $actual.protocol -eq $expected.protocol -and $actual.workspace_id -eq $expected.workspace_id -and $actual.surface_sha256 -eq $expected.surface_sha256 -and $actual.source_matches -eq $true
}
$existing=$null
try { $existing=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 } catch { }
if (Test-WorkspaceIdentity $existing) {
    $null=Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 15
    if (-not $NoBrowser) {Start-Process "http://127.0.0.1:$Port/"}
    Write-Output "Research desk is already running: http://127.0.0.1:$Port/"
    exit 0
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {throw 'Port occupied by another service; no reuse or force stop'}
$indexed=Invoke-StockDataProcess -Executable $Python -Arguments ($prefix+@('journal-index','--ensure-index','--output',$Workspace)) -WorkingDirectory $ReleaseDirectory -TimeoutSeconds 120
if ($indexed.ExitCode -ne 0) {throw 'Journal cache reconciliation failed; original human events retained'}
$browserArgs=@();if (-not $NoBrowser) {$browserArgs=@('--open-browser')}
$serveArgs=$prefix+@('serve','--output',$Workspace,'--port',"$Port")
if ($Background) {
    $logPath=Join-Path $Workspace 'service-logs';$null=New-Item -ItemType Directory -Path $logPath -Force
    $runTag=Get-Date -Format 'yyyyMMdd_HHmmss_fff'
    # Start-Process joins Arguments; quote each path using the same Windows rules.
    $quoted=@($serveArgs | ForEach-Object {$a=[regex]::Replace($_,'(\\*)"','$1$1\"');'"'+[regex]::Replace($a,'(\\+)$','$1$1')+'"'})
    $serviceProcess=Start-Process -FilePath $Python -ArgumentList $quoted -WorkingDirectory $ReleaseDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logPath "$runTag.out.log") -RedirectStandardError (Join-Path $logPath "$runTag.err.log")
    $ready=$false
    for ($attempt=0; $attempt -lt 40; $attempt++) {
        if ($serviceProcess.HasExited) {throw "Workspace failed to start; inspect $logPath\$runTag.err.log"}
        try {
            $health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if (Test-WorkspaceIdentity $health) {
                $null=Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 15
                $ready=$true;break
            }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) {throw 'Workspace did not become healthy; process retained for diagnosis, no force stop'}
    if (-not $NoBrowser) {Start-Process "http://127.0.0.1:$Port/"}
    Write-Output "Current workspace ready: http://127.0.0.1:$Port/ (no acquisition or model training)"
    exit 0
}
& $Python @serveArgs @browserArgs
exit $LASTEXITCODE
