from datetime import datetime, timedelta, timezone
import json

import pytest

from trade_system.artifact_retention import (
    ArtifactBudgetExceeded, finish_test_run, reserve_test_run, retention_plan,
)
from trade_system.file_lock import FileLock, FileLockBusy

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
SOURCE = 'a' * 64


def complete(root, profile='quick', passed=True):
    folder, owner = reserve_test_run(root, profile, SOURCE, now=NOW)
    (folder / 'data.txt').write_text('synthetic fixture')
    finish_test_run(folder, owner, {'passed': passed, 'evidence': 'retained-result.json'})
    return folder, owner


def test_default_artifact_root_is_outside_source_checkout():
    from pathlib import Path
    from trade_system.artifact_retention import owned_test_root
    source = Path(__file__).resolve().parents[1]
    assert not owned_test_root().resolve().is_relative_to(source)


def test_hard_budget_refuses_new_expanded_copy_and_keeps_existing(tmp_path):
    root = tmp_path / 'owned-runs'
    folder, _ = complete(root)
    before = (folder / 'data.txt').read_bytes()
    with pytest.raises(ArtifactBudgetExceeded):
        reserve_test_run(root, 'quick', SOURCE, now=NOW,
                         limits={'max_runs': 1, 'max_files': 100, 'max_bytes': 100000})
    assert (folder / 'data.txt').read_bytes() == before
    assert (root / 'run-registry.guard').exists()


def test_actual_budget_owner_not_pid_or_age_controls_reservation(tmp_path):
    root = tmp_path / 'owned-runs'
    root.mkdir()
    with FileLock(root / 'run-registry.guard'):
        with pytest.raises(FileLockBusy):
            reserve_test_run(root, 'quick', SOURCE, now=NOW)
    assert not [p for p in root.iterdir() if p.is_dir()]


def test_age_alone_never_allows_removal(tmp_path):
    root = tmp_path / 'owned-runs'
    folder, _ = complete(root)
    plan = retention_plan(root, now=NOW + timedelta(days=100))
    assert plan['deletions'] == 0
    assert not plan['decisions'][0]['eligible']
    assert folder.exists()


def test_shared_dependencies_guards_and_unknown_owners_remain_protected(tmp_path):
    root = tmp_path / 'owned-runs'
    folder, _ = complete(root)
    referenced = retention_plan(root, now=NOW + timedelta(days=100), referenced_paths=[folder / 'data.txt'])
    assert referenced['decisions'][0]['reason'] == 'protected_shared_or_active_reference'
    (folder / 'writer.guard').write_bytes(b'')
    guarded = retention_plan(root, now=NOW + timedelta(days=100))
    assert not guarded['decisions'][0]['eligible'] and 'guards_require' in guarded['decisions'][0]['reason']
    unknown = root / 'not-owned'
    unknown.mkdir()
    (unknown / 'human-notes.txt').write_text('must remain')
    plan = retention_plan(root, now=NOW + timedelta(days=100))
    assert not any(row['eligible'] for row in plan['decisions'])
    assert (unknown / 'human-notes.txt').exists()


def test_mismatched_completion_identity_is_not_a_cleanup_permit(tmp_path):
    root = tmp_path / 'owned-runs'
    folder, owner = complete(root)
    finished = json.loads((folder / 'finished.json').read_text())
    finished['run_id'] = 'b' * 32
    (folder / 'finished.json').write_text(json.dumps(finished))
    plan = retention_plan(root, now=NOW + timedelta(days=100), restore_proofs={owner['run_id']: {
        'run_id': owner['run_id'], 'source_sha256': SOURCE,
        'full_archive_sha_verified': True, 'actual_isolated_restore_verified': True}})
    assert not plan['decisions'][0]['eligible']


def test_verified_external_archive_is_only_a_plan_and_detects_source_drift(tmp_path):
    import hashlib
    import zipfile
    root = tmp_path / 'owned-runs'
    folder, owner = complete(root)
    archive = tmp_path / 'original.zip'
    records = {}
    with zipfile.ZipFile(archive, 'x') as z:
        for path in folder.iterdir():
            raw = path.read_bytes()
            records[path.name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            z.writestr(path.name, raw)
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    restored = tmp_path / 'new-isolated-restore'
    restored.mkdir()
    with zipfile.ZipFile(archive) as z:
        for name, record in records.items():
            path = restored / name
            path.write_bytes(z.read(name))
            assert hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256']
    receipt = tmp_path / 'restore-receipt.json'
    receipt.write_text(json.dumps({'schema': 'stockdata_isolated_artifact_restore_v1',
        'run_id': owner['run_id'], 'source_sha256': SOURCE, 'archive_sha256': archive_sha,
        'actual_new_isolated_disk_restore': True, 'files': records}))
    proof = {'run_id': owner['run_id'], 'source_sha256': SOURCE,
             'archive_path': str(archive), 'archive_sha256': archive_sha,
             'restore_receipt_path': str(receipt),
             'restore_receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()}
    plan = retention_plan(root, now=NOW + timedelta(days=100), restore_proofs={owner['run_id']: proof})
    assert plan['decisions'][0]['eligible'] and plan['deletions'] == 0 and folder.exists()
    (folder / 'data.txt').write_text('changed after restore proof')
    changed = retention_plan(root, now=NOW + timedelta(days=100), restore_proofs={owner['run_id']: proof})
    assert not changed['decisions'][0]['eligible']


def test_bare_success_booleans_do_not_prove_a_restorable_archive(tmp_path):
    root = tmp_path / 'owned-runs'
    _, owner = complete(root)
    plan = retention_plan(root, now=NOW + timedelta(days=100), restore_proofs={owner['run_id']: {
        'run_id': owner['run_id'], 'source_sha256': SOURCE,
        'full_archive_sha_verified': True, 'actual_isolated_restore_verified': True}})
    assert not plan['decisions'][0]['eligible']
