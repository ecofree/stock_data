import gzip
import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from trade_system import pipeline_runtime as runtime
from trade_system.file_lock import FileLock, FileLockBusy


def database(tmp_path):
    path = tmp_path / 'synthetic-market.duckdb'
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE TABLE synthetic(value INTEGER)')
        con.execute('INSERT INTO synthetic VALUES (1)')
    return path


def qualified(folder, day):
    archive = folder / ('kpl_data_pre_daily_' + day + '_fixture.duckdb.gz')
    archive.write_bytes(gzip.compress(b'synthetic recovery bytes, not market evidence'))
    receipt = Path(str(archive) + '.qualified.json')
    receipt.write_text(json.dumps({'schema': 1, 'status': 'qualified',
        'qualified_recovery_point': True, 'archive': str(archive.resolve()),
        'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'archive_size': archive.stat().st_size}))
    return archive, receipt


def test_expired_qualified_backups_are_only_planned_never_auto_deleted(tmp_path):
    folder = tmp_path / 'backups'
    folder.mkdir()
    old, old_receipt = qualified(folder, '20260901')
    qualified(folder, '20260902')
    # An external recovery reference must not be erased by quantity-based rotation.
    (tmp_path / 'shared-recovery-index.json').write_text(json.dumps({'archive': str(old)}))
    before = old.read_bytes(), old_receipt.read_bytes()
    result = runtime.retain_qualified_backups(folder, 1, 0)
    assert old.exists() and old_receipt.exists()
    assert (old.read_bytes(), old_receipt.read_bytes()) == before
    assert result['deleted'] == [] and result['automatic_deletion'] is False
    assert old.name in result['candidates_requiring_explicit_review']


def test_backup_capacity_rejects_before_raw_copy_and_preserves_existing(tmp_path, monkeypatch):
    db = database(tmp_path)
    folder = tmp_path / 'backups'
    folder.mkdir()
    partial = folder / 'old-failed.partial'
    partial.write_bytes(b'previous failure evidence')
    monkeypatch.setattr(runtime, 'BACKUP_MAX_BYTES', 1, raising=False)
    with pytest.raises(ValueError, match='backup capacity'):
        runtime.prepare_daily_backup(db, folder, 'blocked')
    assert partial.read_bytes() == b'previous failure evidence'
    assert not list(folder.glob('*.duckdb*'))


def test_failed_and_unknown_backup_files_consume_capacity(tmp_path, monkeypatch):
    db = database(tmp_path)
    folder = tmp_path / 'backups'
    folder.mkdir()
    (folder / 'unqualified.raw').write_bytes(b'x' * 1000)
    monkeypatch.setattr(runtime, 'BACKUP_MAX_FILES', 1, raising=False)
    with pytest.raises(ValueError, match='backup capacity'):
        runtime.prepare_daily_backup(db, folder, 'blocked')
    assert (folder / 'unqualified.raw').exists()


def test_preparation_and_retention_use_actual_shared_folder_guard(tmp_path):
    db = database(tmp_path)
    folder = tmp_path / 'backups'
    folder.mkdir()
    guard = folder / 'backup-maintenance.guard'
    with FileLock(guard):
        with pytest.raises(FileLockBusy):
            runtime.prepare_daily_backup(db, folder, 'busy')
        with pytest.raises(FileLockBusy):
            runtime.retain_qualified_backups(folder)
    assert guard.exists()
    assert not list(folder.glob('*.duckdb*'))


def test_finalization_capacity_failure_keeps_verified_raw(tmp_path, monkeypatch):
    db = database(tmp_path)
    raw = runtime.prepare_daily_backup(db, tmp_path / 'backups', 'first')
    path = Path(raw['raw'])
    before = path.read_bytes()
    monkeypatch.setattr(runtime, 'BACKUP_MAX_BYTES', 1, raising=False)
    with pytest.raises(ValueError, match='backup capacity'):
        runtime.finalize_daily_backup(path)
    assert path.read_bytes() == before
    assert Path(str(path) + '.raw.json').exists()
    assert not Path(str(path) + '.gz.partial').exists()


def test_finalization_rejects_busy_folder_guard(tmp_path):
    db = database(tmp_path)
    raw = runtime.prepare_daily_backup(db, tmp_path / 'backups', 'first')
    path = Path(raw['raw'])
    with FileLock(path.parent / 'backup-maintenance.guard'):
        with pytest.raises(FileLockBusy):
            runtime.finalize_daily_backup(path)
    assert path.exists()
