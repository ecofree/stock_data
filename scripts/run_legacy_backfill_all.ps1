$ErrorActionPreference = 'Continue'
$root = 'D:\accio\stock_data'
$python = 'D:\anaconda\python.exe'
$log = Join-Path $root 'reports\baostock_legacy_unattended.log'
"START $(Get-Date -Format s)" | Out-File -FilePath $log -Encoding utf8
foreach ($spec in @(
    @{Start='20240101'; End='20241231'; Offset=0},
    @{Start='20250101'; End='20251231'; Offset=0}
)) {
    $offset = [int]$spec.Offset
    while ($true) {
        while (Test-Path (Join-Path $root 'kpl_data.duckdb.pipeline.lock')) { Start-Sleep -Seconds 30 }
        $args = @('scripts\backfill_legacy_baostock_parallel.py','--db','kpl_data.duckdb','--start-date',$spec.Start,'--end-date',$spec.End,'--offset',([string]$offset),'--max-stocks','40','--workers','4')
        $text = & $python @args 2>&1 | Out-String
        $text | Add-Content -Path $log -Encoding utf8
        if ($LASTEXITCODE -ne 0) { Start-Sleep -Seconds 60; continue }
        $offset += 40
        if ($text -match "'requested':\s*0") { break }
        if ($text -match "'requested':\s*(\d+)") {
            $requested = [int]$Matches[1]
            if ($requested -lt 40) { break }
        }
    }
}
"END $(Get-Date -Format s)" | Add-Content -Path $log -Encoding utf8
