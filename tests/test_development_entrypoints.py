from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_pre_push_uses_owned_development_runtime_and_explicit_lanes():
    text = (ROOT / 'scripts/pre_push_check.ps1').read_text('utf-8-sig')
    assert 'anaconda' not in text.lower()
    assert '.venv\\python.exe' in text
    assert '--profile release' in text and '--profile research' in text
    assert 'historical_research_CI_required' in text
    assert 'run_tests.py' in text and '-I -B' in text
    assert 'import sys, pytest, ruff, pypdf, lightgbm, qlib' not in text


@pytest.mark.parametrize('name', [
    'run_legacy_backfill_all.ps1', 'run_intraday_push.bat', 'repair_research_batch_logon.ps1',
])
def test_retired_manual_launchers_cannot_reenter_old_runtime_or_account_policy(name):
    text = (ROOT / 'scripts' / name).read_text('utf-8-sig')
    assert 'RETIRED_ENTRYPOINT' in text
    assert 'anaconda' not in text.lower()
    for forbidden in ('Set-LocalUser', 'Enable-LocalUser', 'Disable-LocalUser',
                      'Register-ScheduledTask', 'Start-ScheduledTask', 'while (', '--apply'):
        assert forbidden not in text
    assert ('exit /b 2' in text.lower()) if name.endswith('.bat') else ('throw ' in text)


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='Windows retired entrypoint boundary')
@pytest.mark.parametrize('name,arguments', [
    ('run_legacy_backfill_all.ps1', []), ('run_intraday_push.bat', []),
    *[('repair_research_batch_logon.ps1', ['-Mode', mode])
      for mode in ('Check', 'Apply', 'PrepareAccount', 'ConfigureAccess')],
])
def test_retired_launchers_actually_exit_before_any_side_effect(name, arguments):
    import subprocess
    path = ROOT / 'scripts' / name
    # The precondition deliberately prevents ever running the old unsafe body.
    assert 'RETIRED_ENTRYPOINT' in path.read_text('utf-8-sig')
    command = (['cmd.exe', '/d', '/c', str(path)] if name.endswith('.bat') else
               ['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(path), *arguments])
    result = subprocess.run(command, capture_output=True, timeout=15)
    assert result.returncode != 0
    assert b'RETIRED_ENTRYPOINT' in result.stdout + result.stderr
