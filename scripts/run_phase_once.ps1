param(
    [string]$Db = "kpl_data.duckdb",
    [ValidateSet("auction", "intraday", "close", "supplemental")]
    [string]$Phase = "close",
    [string]$TradeDate = "",
    [switch]$PrepareReference,
    [string]$Python = "",
    [string]$CollectorContract,
    [string]$CollectorContractSha256,
    [string]$ReportsDirectory,
    [string]$ValuationWorkspace='',
    [string]$RuntimeRoot='',
    [string]$EnvironmentFile='',
    [ValidateSet('', 'StockData-ResearchDaily')][string]$PublicationTask=''
)

$ErrorActionPreference = "Stop"
# Keep this a simple script: advanced parameters reserve -Db for -Debug.
if (-not $CollectorContract -or -not $CollectorContractSha256 -or -not $ReportsDirectory) { throw 'Explicit collector contract, hash and reports directory required' }
if ($EnvironmentFile) {
    if (-not [IO.Path]::IsPathRooted($EnvironmentFile) -or -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {throw 'Explicit existing absolute provider environment file required'}
    $env:KPL_ENV_FILE=$EnvironmentFile
}
if ($PublicationTask -and $Phase -ne 'supplemental') { throw 'Only supplemental collection can request follow-up publication' }
if (-not $TradeDate) {
    $TradeDate=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow,'China Standard Time').ToString('yyyy-MM-dd')
}
$runId=$Phase+'_'+[Guid]::NewGuid().ToString('N')
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native_process.ps1')
if ($RuntimeRoot) { Set-StockDataRuntimeRoot $RuntimeRoot }
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
if ($ValuationWorkspace) { $args += @("--valuation-workspace", $ValuationWorkspace) }
if ($TradeDate) { $args += @("--trade-date", $TradeDate) }
if ($PrepareReference) { $args += "--prepare-reference" }

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
    # Legacy evidence can contain an unknown provider as an empty JSON key.
    # Windows PowerShell 5.1 cannot deserialize that object. Read original bytes
    # with Python and project only this handoff's scalar fields; never rewrite
    # the saved receipt or rename its unknown source into a fabricated provider.
    $receiptHash=(Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $readReceiptCode=@'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1])
if p.stat().st_size>20000000: raise ValueError('Collection receipt exceeds read budget')
raw=p.read_bytes()
digest=hashlib.sha256(raw).hexdigest()
if digest!=sys.argv[2]: raise ValueError('Collection receipt changed before handoff')
r=json.loads(raw.decode('utf-8-sig'))
ready=r.get('publication_readiness') or {}
result={k:r.get(k) for k in ('run_id','trade_date','phase','scope','collector_contract_sha256','status')}
if any(v is not None and not isinstance(v,str) for v in result.values()): raise ValueError('Invalid collection receipt binding')
result.update(receipt_sha256=digest,
    normalized_count=sum(s.get('name')=='build_normalized_views' and s.get('status')=='completed' and type(s.get('return_code')) is int and s['return_code']==0 for s in r.get('steps',[])),
    publication_readiness={k:ready.get(k) for k in ('passed','trade_date','reason')})
if any(isinstance(v,(dict,list)) for v in result['publication_readiness'].values()): raise ValueError('Invalid publication readiness handoff')
if ready.get('passed') is not None and type(ready['passed']) is not bool: raise ValueError('Boolean publication readiness required')
print(json.dumps(result,ensure_ascii=True))
'@
    $readResult=Invoke-StockDataProcess -Executable $Python -Arguments @('-B','-c',$readReceiptCode,$receiptPath,$receiptHash) -WorkingDirectory $Root -TimeoutSeconds 30
    if ($readResult.ExitCode -ne 0) {throw ('Collection receipt handoff failed; no publication requested: '+$readResult.Stderr)}
    $receipt=$readResult.Stdout | ConvertFrom-Json
    if ($receipt.receipt_sha256 -ne $receiptHash) {throw 'Collection receipt SHA mismatch; no publication requested'}
    if ($receipt.run_id -ne $runId -or $receipt.trade_date -ne $TradeDate -or $receipt.phase -ne $Phase -or
        $receipt.scope -ne 'transitional_market_collection_only' -or
        $receipt.collector_contract_sha256 -ne $CollectorContractSha256) { throw 'Collection receipt binding mismatch; no publication requested' }
    $ready=$receipt.publication_readiness
    if ($receipt.status -in @('completed','completed_with_warnings','completed_with_certification_gaps','completed_with_degradation') -and $receipt.normalized_count -eq 1 -and
        $ready.passed -eq $true -and $ready.trade_date -eq $TradeDate) {
        $task=Get-ScheduledTask -TaskName $PublicationTask -TaskPath '\' -ErrorAction Stop
        if ($task.State -eq 'Running') { throw 'Publication task already running; request not queued. Retry publication after completion.' }
        $action=@($task.Actions)
        if (-not $task.Settings.Enabled -or $task.Principal.LogonType -ne 'Password' -or $task.Principal.RunLevel -ne 'Limited' -or
            $task.Principal.UserId -in @('SYSTEM','S-1-5-18') -or $action.Count -ne 1 -or
            $action[0].Arguments -notmatch 'run_research_daily\.ps1"' -or $action[0].Arguments -match '-RefreshResearch') {
            throw 'Unified local publication task is not ready; no request made'
        }
        if ((Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256).Hash -ne $receiptHash) {throw 'Collection receipt changed during handoff; no publication requested'}
        Start-ScheduledTask -TaskName $PublicationTask -TaskPath '\' -ErrorAction Stop
        "PUBLICATION_REQUESTED task=$PublicationTask run_id=$runId receipt_sha256=$receiptHash; publication_verified=false" | Tee-Object -FilePath $Log -Append
    } else {
        "PUBLICATION_NOT_REQUESTED run_id=$runId status=$($receipt.status) reason=$($ready.reason)" | Tee-Object -FilePath $Log -Append
    }
}
Write-Output "PHASE_RUN phase=$Phase code=$code log=$Log"
exit $code
