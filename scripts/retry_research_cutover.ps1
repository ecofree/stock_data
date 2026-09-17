[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$installer=Join-Path $PSScriptRoot 'deploy_research_cutover.ps1'
$windowsPowerShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# Each child retains the caller's existing identity; no elevation or execution
# policy bypass. StartMaintenance requires actual interactive confirmation.
# A nonzero exit MUST prevent the next step, including after a partial failure.
foreach ($step in @('StartMaintenance','ArchiveFailure','Check','Apply')) {
    Write-Output "DEPLOYMENT_STEP: $step"
    $arguments=@('-NoProfile')
    if ($step -ne 'StartMaintenance') {$arguments+='-NonInteractive'}
    $arguments+=@('-File',$installer,'-Mode',$step)
    & $windowsPowerShell @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Deployment sequence stopped at $step (exit $LASTEXITCODE). Retain all evidence; do not rerun blindly."
    }
}
Write-Output 'RETRY_SEQUENCE_COMPLETE: installation checks passed; first real scheduled publication still pending.'
