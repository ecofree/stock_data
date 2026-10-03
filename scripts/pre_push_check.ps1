# Local CI gate: same checks as .github/workflows/ci.yml.
# Install once as a pre-push hook:
#   Copy-Item scripts\pre_push_check.ps1 ..\.git\hooks\pre-push -Force
# (git runs hooks via sh; the shim below handles that.)
param(
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
if (-not $Python) { $Python = $env:STOCKDATA_CHECK_PYTHON }
if (-not $Python) { $Python = "D:\anaconda\python.exe" }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Check runtime not found" }
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    & $Python -c "import sys, pytest, ruff, pypdf, lightgbm, qlib; from importlib.metadata import version; parts=version('pypdf').split('.'); sys.exit('patched pypdf 6.19 or newer required') if tuple(int(x) for x in parts[:2]) < (6, 19) else None; print('pre_push_environment: OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Check environment is incomplete. Use -Python or STOCKDATA_CHECK_PYTHON to select an environment with the project's existing test dependencies. No tests were started."
    }
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
