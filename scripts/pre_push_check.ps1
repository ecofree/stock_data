# Local DATA gate; independent historical research remains mandatory in CI.
# The existing Git pre-push shim calls this file. Do not bypass the hook.
param([string]$Python='', [string]$ResearchPython='', [string]$EvidenceDirectory='',
      [string]$VerificationEvidence='', [string]$VerificationSha256='')
$ErrorActionPreference='Stop'
$Root=Split-Path -Parent $PSScriptRoot
if(-not $Python){$Python=$env:STOCKDATA_CHECK_PYTHON}
if(-not $Python){$Python=Join-Path $Root '.venv\python.exe'}
if(-not $ResearchPython){$ResearchPython=$env:STOCKDATA_RESEARCH_CHECK_PYTHON}
if(-not $VerificationEvidence){$VerificationEvidence=$env:STOCKDATA_PREPUSH_EVIDENCE}
if(-not $VerificationSha256){$VerificationSha256=$env:STOCKDATA_PREPUSH_EVIDENCE_SHA256}
if(($VerificationEvidence -and -not $VerificationSha256) -or ($VerificationSha256 -and -not $VerificationEvidence)){throw 'Exact verification path and SHA must be supplied together'}
if(-not $EvidenceDirectory){$EvidenceDirectory=Join-Path ([IO.Path]::GetTempPath()) ('stockdata-prepush-'+[Guid]::NewGuid().ToString('N'))}
if(-not [IO.Path]::IsPathRooted($Python) -or -not(Test-Path -LiteralPath $Python -PathType Leaf)){throw 'Independent development Python required; no global runtime fallback'}
if(Test-Path -LiteralPath $EvidenceDirectory){throw 'New pre-push evidence directory required'}
$env:PYTHONUTF8='1';$env:PYTHONDONTWRITEBYTECODE='1'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Push-Location $Root
try{
 & $Python -I -B -X utf8 -c "import pytest,ruff,pypdf;from importlib.metadata import version;assert tuple(map(int,version('pypdf').split('.')[:2]))>=(6,19);print('pre_push_data_environment: OK')"
 if($LASTEXITCODE -ne 0){throw 'Data-test environment incomplete; no tests started'}
 & $Python -I -B -X utf8 -m pip check
 if($LASTEXITCODE -ne 0){throw 'Development dependency consistency failed'}
 if($VerificationEvidence){
  & $Python -I -B -X utf8 (Join-Path $Root 'tools\v2\verify_pre_push_evidence.py') --proof $VerificationEvidence --sha256 $VerificationSha256
  if($LASTEXITCODE -ne 0){throw 'Provided verification is stale, partial or not this exact source; no bypass'}
  'pre_push_exact_full_gate_reused: actual source/log/JUnit/wheel bytes reverified; tests not duplicated'
 }else{
  & $Python -I -B -X utf8 (Join-Path $Root 'tools\v2\run_tests.py') --profile release --output (Join-Path $EvidenceDirectory 'data')
  if($LASTEXITCODE -ne 0){throw ('Data tests failed; evidence retained at '+$EvidenceDirectory)}
 }
 & $Python -I -B -X utf8 -m ruff check --select E9,F63,F7,F82,F401,F841 .
 if($LASTEXITCODE -ne 0){throw 'ruff failed'}
 & $Python -I -B -X utf8 (Join-Path $Root 'scripts\lint_migrations.py')
 if($LASTEXITCODE -ne 0){throw 'Migration lint failed'}
 & $Python -I -B -X utf8 (Join-Path $Root 'tools\v2\closure_readiness.py') --writer-gate
 if($LASTEXITCODE -ne 0){throw 'Writer boundary failed'}
 if($ResearchPython){
  if(-not [IO.Path]::IsPathRooted($ResearchPython) -or -not(Test-Path -LiteralPath $ResearchPython -PathType Leaf)){throw 'Explicit independent historical research interpreter required'}
  & $ResearchPython -I -B -X utf8 (Join-Path $Root 'tools\v2\run_tests.py') --profile research --output (Join-Path $EvidenceDirectory 'research')
  if($LASTEXITCODE -ne 0){throw 'Independent historical research tests failed'}
 }else{'historical_research_CI_required: NOT VERIFIED LOCALLY; CI must pass before delivery'}
 'pre_push_data_check: OK; not production or complete research acceptance'
}finally{Pop-Location}
