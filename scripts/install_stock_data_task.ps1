# Prepare seven task dispositions; no task, credential, ACL or permit changes.
[CmdletBinding()]
param(
    [switch]$Register, [switch]$RegisterAll,
    [string]$BaselineDirectory, [string]$BaselineInventorySha256,
    [string]$CollectorContract, [string]$CollectorContractSha256,
    [string]$AdapterPython, [string]$ResearchPython,
    [string]$ResearchReleaseDirectory, [string]$ResearchReleaseManifestSha256,
    [string]$EnvironmentFile, [string]$Workspace, [string]$Output,
    [string]$ResearchStartBoundary
)
$ErrorActionPreference='Stop'
if ($Register -or $RegisterAll) { throw 'Legacy task registration retired. Confirm recovery, protected release and a new maintenance window before cutover.' }
foreach ($value in @($BaselineDirectory,$BaselineInventorySha256,$CollectorContract,$CollectorContractSha256,$AdapterPython,$ResearchPython,$ResearchReleaseDirectory,$ResearchReleaseManifestSha256,$EnvironmentFile,$Workspace,$Output)) {
    if (-not $value -or $value -match '["\r\n]') { throw 'Explicit safe baseline, release, environment, interpreter, workspace and output arguments required' }
}
foreach ($digest in @($BaselineInventorySha256,$CollectorContractSha256,$ResearchReleaseManifestSha256)) {
    if ($digest -notmatch '^[a-fA-F0-9]{64}$') { throw 'SHA256 required' }
}
$inventory=Join-Path $BaselineDirectory 'tasks.json'
if ((Get-FileHash -LiteralPath $inventory -Algorithm SHA256).Hash -ne $BaselineInventorySha256) { throw 'Baseline inventory hash mismatch' }
if ((Get-FileHash -LiteralPath $CollectorContract -Algorithm SHA256).Hash -ne $CollectorContractSha256) { throw 'Collection contract hash mismatch' }
$manifest=Join-Path $ResearchReleaseDirectory 'research-release.json'
if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash -ne $ResearchReleaseManifestSha256) { throw 'Research manifest hash mismatch' }
if (Test-Path -LiteralPath $Output) { throw 'New proposal output required; do not overwrite approved evidence' }
$contract=Get-Content -LiteralPath $CollectorContract -Raw | ConvertFrom-Json
if ($contract.scope -ne 'transitional_market_collection_only' -or $contract.execution_ready) { throw 'Unknown collection contract scope' }
if (-not $contract.python -or [IO.Path]::GetFullPath($contract.python) -ne [IO.Path]::GetFullPath($AdapterPython)) { throw 'Adapter Python differs from collector contract' }
foreach ($value in @($contract.database,$contract.reports)) { if (-not $value -or $value -match '["\r\n]') { throw 'Unsafe collection target' } }
$expected=@('StockData-Auction','StockData-Intraday','StockData-DailyClose','StockData-SupplementalRetry','StockData-QLibResearch','StockData-ResearchDaily','StockData-MonthlyCompact')
$baseline=Get-Content -LiteralPath $inventory -Raw | ConvertFrom-Json
$baseline=@($baseline)
if ($baseline.Count -ne 7 -or @($baseline.Name | Select-Object -Unique).Count -ne 7 -or
    @(Compare-Object $expected @($baseline.Name)).Count) { throw 'Exactly seven classified baseline tasks required' }
$xmlByName=@{}; $hashByName=@{}
foreach ($task in $baseline) {
    $path=Join-Path $BaselineDirectory ($task.Name+'.xml')
    # Plain strings only: Windows PowerShell 5 serializes Get-Content's
    # extended PSDrive/provider metadata recursively at a deep JSON depth.
    $raw=[IO.File]::ReadAllText($path)
    $xml=[xml]$raw
    $actions=@($xml.SelectNodes('/*[local-name()="Task"]/*[local-name()="Actions"]/*'))
    $triggers=@($xml.SelectNodes('/*[local-name()="Task"]/*[local-name()="Triggers"]/*'))
    if ($actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec' -or @($task.Actions).Count -ne 1 -or
        [string]$actions[0].Command -cne [string]$task.Actions[0].Execute -or
        [string]$actions[0].Arguments -cne [string]$task.Actions[0].Arguments -or
        [string]$actions[0].WorkingDirectory -cne [string]$task.Actions[0].WorkingDirectory -or
        $triggers.Count -ne @($task.Triggers).Count) { throw ('Baseline XML/inventory mismatch: '+$task.Name) }
    if ($task.Name -in @('StockData-MonthlyCompact','StockData-ResearchDaily') -and
        ($task.State -ne 'Disabled' -or $xml.Task.Settings.Enabled -ne 'false')) { throw ('Retained task must be disabled: '+$task.Name) }
    $xmlByName[$task.Name]=$raw
    $hashByName[$task.Name]=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
}
$identity=([xml]$xmlByName['StockData-ResearchDaily']).Task.Principals.Principal
if ($identity.UserId -notmatch '^S-1-5-21-' -or $identity.LogonType -ne 'Password' -or
    ($identity.RunLevel -and $identity.RunLevel -ne 'LeastPrivilege')) { throw 'Retained dedicated Limited/Password identity required, never SYSTEM' }
$repo=Split-Path -Parent $PSScriptRoot
$common=' -Db "'+$contract.database+'" -Python "'+$AdapterPython+'" -CollectorContract "'+$CollectorContract+'" -CollectorContractSha256 '+$CollectorContractSha256+' -ReportsDirectory "'+$contract.reports+'"'
$prefix='-NoProfile -NonInteractive -File "'
$research=$prefix+(Join-Path $PSScriptRoot 'run_research_daily.ps1')+'" -Python "'+$ResearchPython+'" -Workspace "'+$Workspace+'" -ReleaseDirectory "'+$ResearchReleaseDirectory+'" -ReleaseManifestSha256 '+$ResearchReleaseManifestSha256
$actions=@{
    'StockData-Auction'=$prefix+(Join-Path $PSScriptRoot 'run_phase_watch.ps1')+'" -Phase auction -IntervalSeconds 120 -EndAt 09:27 -ContinueOnFailure'+$common
    'StockData-Intraday'=$prefix+(Join-Path $PSScriptRoot 'run_phase_watch.ps1')+'" -Phase intraday -IntervalSeconds 300 -EndAt 15:05 -SkipLunch -ContinueOnFailure'+$common
    'StockData-DailyClose'=$prefix+(Join-Path $PSScriptRoot 'run_stock_data_daily.ps1')+'" -Phase close -CollectionProfile priority'+$common
    'StockData-SupplementalRetry'=$prefix+(Join-Path $PSScriptRoot 'run_phase_once.ps1')+'" -Phase supplemental -PublicationTask StockData-ResearchDaily'+$common
    'StockData-ResearchDaily'=$research
    'StockData-QLibResearch'=$research+' -RefreshResearch -EnvironmentFile "'+$EnvironmentFile+'"'
}
$rows=@(foreach ($name in $expected) {
    $existing=@($baseline | Where-Object Name -eq $name)[0]
    $preserve=$name -eq 'StockData-MonthlyCompact'
    $isResearch=$name -in @('StockData-ResearchDaily','StockData-QLibResearch')
    [pscustomobject]@{
        Name=$name;Disposition=$(if($preserve){'preserve_disabled'}else{'replace_responsibility'});
        BeforeState=$existing.State;BeforeXml=$xmlByName[$name];BeforeXmlSha256=$hashByName[$name];
        Execute=$(if($preserve){$existing.Actions[0].Execute}else{Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'});
        Arguments=$(if($preserve){$existing.Actions[0].Arguments}else{$actions[$name]});
        WorkingDirectory=$(if($preserve){$existing.Actions[0].WorkingDirectory}else{$repo});
        Schedule=$(if($name -eq 'StockData-ResearchDaily'){'proposed weekdays 19:30 plus on-demand request after supplemental; exact start date requires new approval'}else{'preserve existing triggers'});
        Principal=$(if($isResearch){[ordered]@{UserId=[string]$identity.UserId;LogonType='Password';RunLevel='Limited'}}else{[ordered]@{Mode='preserve_exact_xml';Definition=([xml]$xmlByName[$name]).Task.Principals.OuterXml}});
        IdentityRule=$(if($isResearch){'dedicated Limited/Password, never SYSTEM'}else{'preserve existing identity'});
        Responsibility=$(if($preserve){'no compaction; retained disabled'}elseif($name -eq 'StockData-QLibResearch'){'current research product frozen-model update; no fit, promotion, legacy registry or page callback'}elseif($isResearch){'local market snapshot -> unified daily workspace; no provider requests or model fits'}else{'collection and diagnostics only; supplemental requests unified publication after releasing database lock'});
        Rollback='restore exact BeforeXml and original enabled/disabled state under separately approved authenticated rollback; no database rollback or deletion of human notes'
    }
})
$files=[ordered]@{}
foreach ($relative in @('scripts/install_stock_data_task.ps1','scripts/deploy_current_tasks.ps1','scripts/run_integrated_daily.py','scripts/run_phase_once.ps1','scripts/run_phase_watch.ps1','scripts/run_stock_data_daily.ps1','scripts/run_research_daily.ps1','scripts/native_process.ps1','trade_system/source_authority.py','trade_system/collection_profiles.py')) {
    $files[$relative]=(Get-FileHash -LiteralPath (Join-Path $repo $relative) -Algorithm SHA256).Hash
}
$proposal=[ordered]@{Schema=2;Scope='task_handover_proposal_only';CapturedAt=[DateTimeOffset]::UtcNow.ToString('o');
    SystemChanges=0;ProductionCutover=$false;Actions=$rows;AdapterFiles=$files;
    BaselineInventorySha256=$BaselineInventorySha256;BaselineXmlSha256=$hashByName;
    CollectionContractSha256=$CollectorContractSha256;ResearchManifestSha256=$ResearchReleaseManifestSha256;
    MonthlyCompact='retain disabled; no changes';
    Blockers=@('baseline is historical: compare all seven live XML definitions immediately before cutover',
        'fresh recovery and ACL rollback verification required',
        'protect exact collector/research runtimes, release and provider environment before enabling tasks',
        'dedicated identity must pass offline probe and authenticated task-update/rollback rehearsal',
        'current transaction engine has no live backend; offline rehearsal is not Windows authentication or approval',
        'confirm exact new maintenance window and seven-task scope before applying')}
if ($ResearchStartBoundary) {
    $releaseMetadata=[IO.File]::ReadAllText($manifest) | ConvertFrom-Json
    if ($releaseMetadata.version -ne '0.3.15') {throw 'Compiled transaction requires the fixed 0.3.15 research release'}
    . (Join-Path $PSScriptRoot 'deploy_current_tasks.ps1') -Mode Library
    $proposal.Schema=3;$proposal.Scope='task_handover_engineering_only'
    $proposal.Version='0.3.15';$proposal.ResearchStartBoundary=$ResearchStartBoundary
    $proposal.AuthenticationVerified=$false;$proposal.LiveBackendEnabled=$false
    foreach ($row in $rows) {$row | Add-Member NoteProperty AfterXml (Replacement-Xml $row $ResearchStartBoundary)}
    Assert-EngineeringPlan $proposal
}
$parent=Split-Path -Parent ([IO.Path]::GetFullPath($Output))
if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
$stream=[IO.File]::Open($Output,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try {$bytes=[Text.UTF8Encoding]::new($false).GetBytes(($proposal | ConvertTo-Json -Depth 12));$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
Write-Output "TASK_PROPOSAL_SAVED: $Output ; system changes=0; not a deployment approval."
