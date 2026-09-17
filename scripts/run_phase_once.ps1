param(
    [string]$Db = "kpl_data.duckdb",
    [ValidateSet("auction", "intraday", "close", "supplemental")]
    [string]$Phase = "close",
    [string]$TradeDate = "",
    [string]$Python = "",
    [string]$CollectorContract,
    [string]$CollectorContractSha256,
    [string]$ReportsDirectory,
    [ValidateSet('', 'StockData-ResearchDaily')][string]$PublicationTask=''
)

$ErrorActionPreference = "Stop"
# Keep this a simple script: advanced parameters reserve -Db for -Debug.
if (-not $CollectorContract -or -not $CollectorContractSha256 -or -not $ReportsDirectory) { throw 'Explicit collector contract, hash and reports directory required' }
if ($PublicationTask -and $Phase -ne 'supplemental') { throw 'Only supplemental collection can request follow-up publication' }
if (-not $TradeDate) {
    $TradeDate=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow,'China Standard Time').ToString('yyyy-MM-dd')
}
$runId=$Phase+'_'+[Guid]::NewGuid().ToString('N')
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native_process.ps1')
if (-not $Python) { $Python = Join-Path $Root '.venv\Scripts\python.exe' }
$IntegratedRunner = Join-Path $Root "scripts\run_integrated_daily.py"
$DbPath = if ([System.IO.Path]::IsPathRooted($Db)) { $Db } else { Join-Path $Root $Db }
$LogDir = Join-Path $ReportsDirectory "scheduled-logs"
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
    "--reports-dir", $ReportsDirectory,
    "--collector-contract", $CollectorContract,
    "--collector-contract-sha256", $CollectorContractSha256,
    "--phase", $Phase, "--run-id", $runId,
    "--collection-profile", "priority"
)
if ($TradeDate) { $args += @("--trade-date", $TradeDate) }

Push-Location $Root
try {
    $result = Invoke-StockDataProcess -Executable $Python -Arguments $args -WorkingDirectory $Root
    $code = $result.ExitCode
    ($result.Stdout + $result.Stderr) | Tee-Object -FilePath $Log -Append
} finally {
    Pop-Location
}
if ($null -eq $code) { $code = 1 }
# The child has exited and released its database lock. A unique structured
# receipt, not console text or an old latest file, permits a publication request.
if ($PublicationTask -and $code -in @(0,2)) {
    $receiptPath=Join-Path $ReportsDirectory ('runs\'+$runId+'\run.json')
    $receipt=Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    if ($receipt.run_id -ne $runId -or $receipt.trade_date -ne $TradeDate -or $receipt.phase -ne $Phase -or
        $receipt.scope -ne 'transitional_market_collection_only' -or
        $receipt.collector_contract_sha256 -ne $CollectorContractSha256) { throw 'Collection receipt binding mismatch; no publication requested' }
    $normalized=@($receipt.steps | Where-Object { $_.name -eq 'build_normalized_views' -and $_.status -eq 'completed' -and $_.return_code -eq 0 })
    $collected=@($receipt.steps | Where-Object { $_.name -in @('collect_lhb_daily','collect_auction_market_daily','collect_index_kline_daily','collect_xiaodefa_critical') -and $_.status -eq 'completed' -and $_.return_code -eq 0 })
    if ($receipt.status -in @('completed','completed_with_degradation') -and $normalized.Count -eq 1 -and $collected.Count -gt 0) {
        $task=Get-ScheduledTask -TaskName $PublicationTask -TaskPath '\' -ErrorAction Stop
        if ($task.State -eq 'Running') { throw 'Publication task already running; request not queued. Retry publication after completion.' }
        $action=@($task.Actions)
        if (-not $task.Settings.Enabled -or $task.Principal.LogonType -ne 'Password' -or $task.Principal.RunLevel -ne 'Limited' -or
            $task.Principal.UserId -in @('SYSTEM','S-1-5-18') -or $action.Count -ne 1 -or
            $action[0].Arguments -notmatch 'run_research_daily\.ps1"' -or $action[0].Arguments -match '-RefreshResearch') {
            throw 'Unified local publication task is not ready; no request made'
        }
        Start-ScheduledTask -TaskName $PublicationTask -TaskPath '\' -ErrorAction Stop
        "PUBLICATION_REQUESTED task=$PublicationTask run_id=$runId; publication_verified=false" | Tee-Object -FilePath $Log -Append
    } else {
        "PUBLICATION_NOT_REQUESTED run_id=$runId status=$($receipt.status)" | Tee-Object -FilePath $Log -Append
    }
}
Write-Output "PHASE_RUN phase=$Phase code=$code log=$Log"
exit $code
