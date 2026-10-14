"""Bounded owned test artifacts and conservative retention planning.

This is not an automatic eraser. Shared archives, guards, unknown ownership,
incomplete runs and missing restore proofs always remain protected.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from trade_system.file_lock import FileLock

DEFAULT_LIMITS = {'max_runs': 12, 'max_files': 100000, 'max_bytes': 10 * 1024**3}
DEFAULT_DAYS = {'quick': 7, 'release': 14, 'history': 30, 'research': 30, 'stress': 3, 'all': 14}


def owned_test_root():
    # Deliberately outside checkout: synthetic runtime-layout tests must obey
    # the same outside-source boundary as the actual deployed runtime.
    return Path(tempfile.gettempdir()) / 'stockdata-owned-test-runs'


class ArtifactBudgetExceeded(ValueError):
    pass


def _safe(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('artifact paths must not traverse links')
    return path


def inventory(root):
    root = _safe(root)
    files, size, runs = 0, 0, 0
    if root.exists():
        for path in root.rglob('*'):
            _safe(path)
            if path.is_file():
                files += 1
                size += path.stat().st_size
            if path.parent == root and path.is_dir():
                runs += 1
    return {'runs': runs, 'files': files, 'bytes': size}


def reserve_test_run(root, profile, source_sha256, *, now=None, limits=None):
    root = _safe(root)
    if profile not in DEFAULT_DAYS or not re.fullmatch('[a-f0-9]{64}', source_sha256):
        raise ValueError('known profile and exact source identity required')
    limits = dict(DEFAULT_LIMITS if limits is None else limits)
    if set(limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in limits.values()):
        raise ValueError('positive bounded artifact limits required')
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError('aware artifact clock required')
    root.mkdir(parents=True, exist_ok=True)
    guard = _safe(root / 'run-registry.guard')
    with FileLock(guard):
        existing = inventory(root)
        if any(existing[key] >= limits['max_' + key] for key in ('runs', 'files', 'bytes')):
            raise ArtifactBudgetExceeded('owned test artifact budget reached; inspect retention plan, do not create another expanded copy')
        run_id = uuid4().hex
        folder = root / run_id
        folder.mkdir(exist_ok=False)
        owner = {'schema': 'stockdata_owned_test_run_v1', 'run_id': run_id,
                 'profile': profile, 'source_sha256': source_sha256,
                 'created_at': clock.isoformat(), 'scope': 'synthetic_test_artifacts_only',
                 'production_authority': False}
        (folder / 'owner.json').write_text(json.dumps(owner, sort_keys=True) + '\n', encoding='utf-8')
        return folder, owner


def finish_test_run(folder, owner, verification):
    folder = _safe(folder)
    actual = json.loads((folder / 'owner.json').read_text('utf-8'))
    if actual != owner or folder.name != owner['run_id']:
        raise ValueError('test artifact ownership changed')
    if type(verification.get('passed')) is not bool:
        raise ValueError('explicit test result required')
    result = {'schema': 'stockdata_owned_test_completion_v1', 'run_id': owner['run_id'],
              'source_sha256': owner['source_sha256'], 'finished_at': datetime.now(timezone.utc).isoformat(),
              'passed': verification['passed'], 'evidence': verification.get('evidence'),
              'release_gate_complete': False, 'business_acceptance': False}
    with (folder / 'finished.json').open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, sort_keys=True) + '\n')
    return result


def _verified_restore(proof, owner, folder):
    """Check actual pinned artifacts and the still-current source set, not flags alone."""
    if (not isinstance(proof, dict) or proof.get('run_id') != owner['run_id']
            or proof.get('source_sha256') != owner['source_sha256']):
        return False
    try:
        paths = {}
        for key in ('archive', 'restore_receipt'):
            path = _safe(proof[key + '_path'])
            expected = proof[key + '_sha256']
            if path.is_relative_to(folder) or not re.fullmatch('[a-f0-9]{64}', expected):
                return False
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    return False
            paths[key] = path
        if paths['restore_receipt'].stat().st_size > 2_000_000:
            return False
        receipt = json.loads(paths['restore_receipt'].read_text('utf-8'))
        if (receipt.get('schema') != 'stockdata_isolated_artifact_restore_v1'
                or receipt.get('run_id') != owner['run_id']
                or receipt.get('source_sha256') != owner['source_sha256']
                or receipt.get('archive_sha256') != proof['archive_sha256']
                or receipt.get('actual_new_isolated_disk_restore') is not True):
            return False
        expected_files = receipt['files']
        actual = {p.relative_to(folder).as_posix(): p for p in folder.rglob('*') if p.is_file()}
        if not expected_files or set(actual) != set(expected_files):
            return False
        for name, path in actual.items():
            _safe(path)
            record = expected_files[name]
            if path.stat().st_size != record['bytes']:
                return False
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                    return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def retention_plan(root, *, now=None, referenced_paths=(), restore_proofs=None):
    root = _safe(root)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError('aware retention clock required')
    references = [_safe(p) for p in referenced_paths]
    proofs = restore_proofs or {}
    decisions = []
    if not root.exists():
        return {'scope': 'readonly_retention_plan', 'decisions': [], 'deletions': 0}
    for folder in sorted(root.iterdir()):
        _safe(folder)
        if not folder.is_dir():
            continue
        reason, eligible = 'unknown_or_incomplete_owned_run', False
        owner_path, finish_path = folder / 'owner.json', folder / 'finished.json'
        try:
            owner = json.loads(owner_path.read_text('utf-8'))
            finish = json.loads(finish_path.read_text('utf-8'))
            created = datetime.fromisoformat(owner['created_at'])
            if (owner['schema'] != 'stockdata_owned_test_run_v1' or owner['run_id'] != folder.name
                    or owner.get('scope') != 'synthetic_test_artifacts_only'
                    or owner.get('production_authority') is not False
                    or not re.fullmatch('[a-f0-9]{64}', owner['source_sha256'])
                    or not re.fullmatch('[a-f0-9]{32}', owner['run_id']) or created.tzinfo is None
                    or finish.get('schema') != 'stockdata_owned_test_completion_v1'
                    or finish.get('run_id') != owner['run_id']
                    or finish.get('source_sha256') != owner['source_sha256']
                    or type(finish.get('passed')) is not bool):
                raise ValueError('ownership/completion mismatch')
            days = DEFAULT_DAYS[owner['profile']] if finish['passed'] else 30
            if clock < created + timedelta(days=days):
                reason = 'within_retention_period'
            else:
                reason = 'expired_requires_verified_archive_and_isolated_restore'
                proof = proofs.get(owner['run_id'], {})
                if _verified_restore(proof, owner, folder):
                    eligible, reason = True, 'eligible_for_separately_reviewed_verified_retirement'
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if any(p == folder or p.is_relative_to(folder) or folder.is_relative_to(p) for p in references):
            eligible, reason = False, 'protected_shared_or_active_reference'
        guards = []
        for path in folder.rglob('*'):
            _safe(path)
            if path.name.endswith('.guard'):
                guards.append(path.relative_to(folder).as_posix())
        if guards:
            eligible, reason = False, 'permanent_guards_require_same_identity_retention_not_deletion'
        decisions.append({'path': str(folder), 'eligible': eligible, 'reason': reason,
                          'guards': guards, 'owner_sha256': hashlib.sha256(owner_path.read_bytes()).hexdigest()
                          if owner_path.is_file() else None})
    return {'scope': 'readonly_retention_plan', 'evaluated_at': clock.isoformat(),
            'decisions': decisions, 'deletions': 0, 'age_alone_never_authorizes_deletion': True}
