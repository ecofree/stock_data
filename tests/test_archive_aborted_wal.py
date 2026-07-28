from __future__ import annotations

import zipfile

from scripts.archive_aborted_wal import archive_aborted_wal


def test_archive_aborted_wal_dry_run_does_not_delete(tmp_path):
    db = tmp_path / "kpl_data.duckdb"
    db.touch()
    item = tmp_path / "kpl_data.duckdb.wal.aborted-test"
    item.write_bytes(b"wal")

    result = archive_aborted_wal(db)

    assert result["status"] == "dry_run"
    assert item.exists()


def test_archive_aborted_wal_apply_verifies_then_removes(tmp_path):
    db = tmp_path / "kpl_data.duckdb"
    db.touch()
    item = tmp_path / "kpl_data.duckdb.wal.aborted-test"
    item.write_bytes(b"wal")
    archive = tmp_path / "backups" / "wal.zip"

    result = archive_aborted_wal(db, archive, apply=True)

    assert result["status"] == "archived"
    assert not item.exists()
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.read(item.name) == b"wal"
