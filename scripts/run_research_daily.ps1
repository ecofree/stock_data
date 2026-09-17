[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Python,
      [string]$EnvironmentFile='',
      [string]$ExpectedTradeDate='',
      [switch]$RefreshResearch,
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
if ($RefreshResearch -and -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) { throw 'Explicit provider environment file is required for separate research refresh' }
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
if ($RefreshResearch) { $env:KPL_ENV_FILE=$EnvironmentFile }
$env:PYTHONUTF8='1'
. (Join-Path $PSScriptRoot 'native_process.ps1')
$dependency=Invoke-StockDataProcess -Executable $Python -Arguments @('-I','-B','-m','pip','check') -WorkingDirectory $projectPath
if ($dependency.ExitCode -ne 0) { throw 'Research dependency check failed; no fallback runtime or publication permitted' }
if (-not $ExpectedTradeDate) {
    $ExpectedTradeDate=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow,'China Standard Time').ToString('yyyy-MM-dd')
}
if ($ExpectedTradeDate -notmatch '^\d{4}-\d{2}-\d{2}$') { throw 'Expected ISO market date required' }
$runLogDirectory=Join-Path $Workspace 'scheduled-logs'
New-Item -ItemType Directory -Path $runLogDirectory -Force | Out-Null
$runLog=Join-Path $runLogDirectory ((Get-Date -Format 'yyyyMMdd_HHmmss_fff')+'.log')
$prefix=if ($ReleaseDirectory) { @('-I','-X','utf8','-B',$launcher) } else { @('-X','utf8','-B','-m','trade_system.v2.research_product') }
# Daily market publication is local-data-only. It never waits for provider
# acquisition, retraining, or promotion of a research model.
$market=Invoke-StockDataProcess -Executable $Python -Arguments ($prefix+@('market-update','--output',$Workspace,'--expected-date',$ExpectedTradeDate)) -WorkingDirectory $projectPath
($market.Stdout+$market.Stderr) | Tee-Object -FilePath $runLog
if ($market.ExitCode -ne 0) { throw "Market publication failed ($($market.ExitCode)); prior verified page retained. Log: $runLog" }
$marketReceipt=$market.Stdout.Trim() | ConvertFrom-Json
if ($marketReceipt.status -eq 'market_closed') {
    Write-Output "MARKET_CLOSED expected_date=$ExpectedTradeDate; provider_requests=0; no research refresh."
    exit 0
}
if ($marketReceipt.status -ne 'market_published' -or $marketReceipt.date -ne $ExpectedTradeDate -or
    -not $marketReceipt.snapshot_id -or $marketReceipt.provider_requests -ne 0 -or $marketReceipt.fits -ne 0) {
    throw 'Local market receipt is not current and verified; no research refresh'
}
if ($RefreshResearch) {
    $research=Invoke-StockDataProcess -Executable $Python -Arguments ($prefix+@('update','--output',$Workspace)) -WorkingDirectory $projectPath
    ($research.Stdout+$research.Stderr) | Tee-Object -FilePath $runLog -Append
    if ($research.ExitCode -ne 0) { throw "Research refresh failed ($($research.ExitCode)); market publication remains available. Log: $runLog" }
}
Write-Output "DAILY_WORKSPACE_COMPLETE expected_date=$ExpectedTradeDate; no trading, account import or task registration."
exit 0
