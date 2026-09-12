[CmdletBinding()]
param([string]$Python='D:\anaconda\python.exe',
      [string]$EnvironmentFile='D:\accio\stock_data\.env',
      [string]$Workspace='',
      [string]$ReleaseDirectory='',
      [string]$ReleaseManifestSha256='')
$ErrorActionPreference='Stop'
$projectPath=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectPath
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Verified research Python is required' }
if (-not $Workspace) { $Workspace=Join-Path $projectPath 'reports/research-delivery' }
$Workspace=(Resolve-Path -LiteralPath $Workspace -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath (Join-Path $Workspace 'workspace-config.json') -PathType Leaf)) {
    throw 'Configure the read-only market database before scheduling research updates'
}
if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) { throw 'Explicit provider environment file is required' }
if ([bool]$ReleaseDirectory -ne [bool]$ReleaseManifestSha256) { throw 'Release directory and approved manifest hash must be supplied together' }
if ($ReleaseDirectory) {
    if ($ReleaseManifestSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid approved release hash' }
    $ReleaseDirectory=(Resolve-Path -LiteralPath $ReleaseDirectory -ErrorAction Stop).Path
    $releasePrefix=$ReleaseDirectory.TrimEnd('\')+'\'
    if ($Workspace.Equals($ReleaseDirectory,[StringComparison]::OrdinalIgnoreCase) -or $Workspace.StartsWith($releasePrefix,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Mutable workspace must be outside the immutable release'
    }
    $manifestPath=Join-Path $ReleaseDirectory 'research-release.json'
    if ((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash -ne $ReleaseManifestSha256) {
        throw 'Release manifest differs from approved hash; no update performed'
    }
    $launcher=Join-Path $ReleaseDirectory 'run_research.py'
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Verified release launcher required' }
}
$env:KPL_ENV_FILE=$EnvironmentFile
$env:PYTHONUTF8='1'
$runLogDirectory=Join-Path $Workspace 'scheduled-logs'
New-Item -ItemType Directory -Path $runLogDirectory -Force | Out-Null
$runLog=Join-Path $runLogDirectory ((Get-Date -Format 'yyyyMMdd_HHmmss_fff')+'.log')
if ($ReleaseDirectory) {
    & $Python -I -X utf8 -B $launcher update --output $Workspace 2>&1 | Tee-Object -FilePath $runLog
} else {
    & $Python -X utf8 -B -m trade_system.v2.research_product update --output $Workspace 2>&1 | Tee-Object -FilePath $runLog
}
$resultCode=$LASTEXITCODE
if ($resultCode -ne 0) { throw "Research update failed ($resultCode); prior verified publication is retained. Log: $runLog" }
Write-Output 'Research-only publication completed. No task registration, account import or order execution occurred.'
exit 0
