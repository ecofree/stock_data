import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.v2.deployment_probe import write_denied


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/deploy_research_cutover.ps1'


def test_probe_never_modifies_a_file_when_write_is_unexpectedly_allowed(tmp_path):
    path = tmp_path / 'source'; path.write_bytes(b'protected original')
    assert write_denied(path) is False
    assert path.read_bytes() == b'protected original'


def test_probe_only_accepts_access_denied_not_sharing_violations(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        exc = PermissionError('denied'); exc.winerror = 5; raise exc
    monkeypatch.setattr(Path, 'open', denied)
    assert write_denied(tmp_path / 'source') is True
    def sharing(*args, **kwargs):
        exc = PermissionError('busy'); exc.winerror = 32; raise exc
    monkeypatch.setattr(Path, 'open', sharing)
    assert write_denied(tmp_path / 'source') is False


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer syntax')
def test_powershell_installer_parses_and_window_is_exact():
    command = r'''
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
if ($errors.Count) { $errors | ForEach-Object { Write-Output $_.Message };exit 1 }
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-Window'},$true)
Invoke-Expression $fn.Extent.Text
foreach ($stamp in @('2026-09-13T15:59:59+08:00','2026-09-13T17:00:00+08:00','2026-09-14T16:00:00+08:00')) {
  $rejected=$false;try {Assert-Window ([DateTimeOffset]::Parse($stamp))} catch {$rejected=$true}
  if (-not $rejected) {exit 2}
}
Assert-Window ([DateTimeOffset]::Parse('2026-09-13T16:00:00+08:00'))
Assert-Window ([DateTimeOffset]::Parse('2026-09-13T16:59:59+08:00'))
Write-Output 'syntax_and_window_pass'
'''.replace('__SCRIPT__', str(SCRIPT))
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True,
                            text=True, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'syntax_and_window_pass' in result.stdout


def test_installer_has_no_destructive_cleanup_or_system_principal_fallback():
    text = SCRIPT.read_text()
    for forbidden in ('Stop-Process', 'Stop-ScheduledTask', 'Remove-LocalUser', 'Remove-Item', '-ExecutionPolicy Bypass', '-UserId "SYSTEM"'):
        assert forbidden not in text
    assert "Assert-Admin; Assert-Window" in text
    assert "-UserMayNotChangePassword" in text
    assert "-RunLevel Limited" in text
    assert "$trigger.StartBoundary='2026-09-14T18:30:00+08:00'" in text
    assert 'Assert-Hash $backup $entry.before_sha256' in text
    assert 'post-apply artifact audit failed' in text.lower()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows byte-range lock protocol')
def test_powershell_guard_conflicts_with_the_real_python_guard(tmp_path):
    guard = tmp_path / 'existing.guard'; guard.touch()
    command = r'''
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
foreach ($name in @('Assert-PlainPath','Hold-Guard')) {
 $fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 Invoke-Expression $fn.Extent.Text
}
$guards=[Collections.Generic.List[IO.FileStream]]::new()
Hold-Guard '__GUARD__'
try {
 & '__PYTHON__' -B -c "from trade_system.file_lock import FileLock,FileLockBusy; import sys; p=sys.argv[1];`ntry:`n with FileLock(p): pass`nexcept FileLockBusy: sys.exit(3)" '__GUARD__'
 if ($LASTEXITCODE -ne 3) {exit 4}
} finally {foreach ($g in $guards) {$g.Dispose()}}
& '__PYTHON__' -B -c "from trade_system.file_lock import FileLock; import sys;`nwith FileLock(sys.argv[1]): pass" '__GUARD__'
exit $LASTEXITCODE
'''.replace('__SCRIPT__', str(SCRIPT)).replace('__GUARD__', str(guard)).replace('__PYTHON__', sys.executable)
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True,
                            text=True, cwd=ROOT, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert guard.exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows directory access probe')
def test_directory_probe_opens_existing_handle_without_creating_a_note(tmp_path):
    from tools.v2.deployment_probe import directory_write_denied
    assert directory_write_denied(tmp_path) is False
    assert list(tmp_path.iterdir()) == []
