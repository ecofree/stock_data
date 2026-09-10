"""First-run logging must work without pre-existing local runtime folders."""
import subprocess
import sys
from pathlib import Path


def test_base_import_creates_missing_log_directory(tmp_path):
    target = tmp_path / 'new-runtime' / 'logs'
    code = (
        'import config, sys; '
        'config.LOG_DIR = sys.argv[1]; '
        'import base; '
        "base.logger.warning('clean import regression')"
    )
    result = subprocess.run(
        [sys.executable, '-c', code, str(target)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    logs = list(target.glob('collect_*.log'))
    assert len(logs) == 1
    assert 'clean import regression' in logs[0].read_text(encoding='utf-8')
