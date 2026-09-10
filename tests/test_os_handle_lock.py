"""Real process/OS tests, including Windows; never target the production DB."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from trade_system.file_lock import FileLock, FileLockBusy
from trade_system.pipeline_runtime import PipelineLock


ROOT = Path(__file__).resolve().parents[1]


def child(db, source):
    return subprocess.run([sys.executable, '-c', source, str(db)], cwd=ROOT,
                          capture_output=True, text=True, timeout=30)


def test_real_competitor_cannot_acquire_or_kill_healthy_owner(tmp_path):
    db = tmp_path / 'only_fixture.duckdb'
    with PipelineLock(db, 'healthy'):
        result = child(db, '''
import sys
from trade_system.pipeline_runtime import PipelineLock, PipelineAlreadyRunning
try:
    with PipelineLock(sys.argv[1], 'competitor'):
        sys.exit(9)
except PipelineAlreadyRunning:
    print('blocked')
''')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'blocked'
    with PipelineLock(db, 'next'):
        pass


def test_process_death_releases_handle_without_unlink_or_pid_probe(tmp_path, monkeypatch):
    db = tmp_path / 'crash_fixture.duckdb'
    result = child(db, '''
import os, sys
from trade_system.pipeline_runtime import PipelineLock
with PipelineLock(sys.argv[1], 'crashed'):
    os._exit(17)
''')
    assert result.returncode == 17
    metadata = db.with_name(db.name + '.pipeline.lock')
    guard = metadata.with_suffix(metadata.suffix + '.guard')
    assert metadata.exists() and guard.exists()
    identity = guard.stat().st_ino
    monkeypatch.setattr(os, 'kill', lambda *a: pytest.fail('PID probing is forbidden'))
    with PipelineLock(db, 'recovered'):
        assert guard.stat().st_ino == identity
    assert not metadata.exists()
    assert guard.exists()


def test_nested_lock_object_keeps_original_handle(tmp_path):
    lock = FileLock(tmp_path / 'same.guard')
    with lock:
        fd = lock.fd
        with pytest.raises(FileLockBusy):
            lock.__enter__()
        assert lock.fd == fd
        with pytest.raises(FileLockBusy):
            with FileLock(lock.path):
                pass


def test_inaccessible_lock_does_not_create_permission_or_remove_target(tmp_path, monkeypatch):
    lock = FileLock(tmp_path / 'denied.guard')
    def denied(*a, **kw):
        raise PermissionError('fixture access denied')
    monkeypatch.setattr(os, 'open', denied)
    with pytest.raises(PermissionError):
        lock.__enter__()
    assert lock.fd is None
