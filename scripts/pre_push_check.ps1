# Local CI gate: same checks as .github/workflows/ci.yml.
# Install once as a pre-push hook:
#   Copy-Item scripts\pre_push_check.ps1 ..\.git\hooks\pre-push -Force
# (git runs hooks via sh; the shim below handles that.)
param(
    [string]$Python = "D:\anaconda\python.exe"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    & $Python -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
    # Same rule set as pyproject.toml / .github/workflows/ci.yml.
    & $Python -m ruff check --select E9,F63,F7,F82,F401,F841 .
    if ($LASTEXITCODE -ne 0) { throw "ruff failed" }
    & $Python (Join-Path $Root "scripts\lint_migrations.py")
    if ($LASTEXITCODE -ne 0) { throw "migration lint failed" }
    "pre_push_check: OK"
} finally {
    Pop-Location
}
