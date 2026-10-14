"""Offline custody of explicitly reviewed valuation inputs, not business import.

Plans are read-only. Admission holds a permanent OS guard, publishes originals
by content hash without replacement, and writes the reviewed manifest last.
A crash may leave a hash-bound, resumable intent; it never grants qualification.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from trade_system.file_lock import FileLock

MAX_ORIGINAL = 32_000_000


def _canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def plan_sha256(plan):
    return hashlib.sha256(_canonical(plan)).hexdigest()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('intake rejects linked paths')
    return path


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('exact lowercase SHA256 required')
    return value


def _read(path, expected, limit):
    path = _path(path)
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit or before.st_nlink != 1:
            raise ValueError('intake size/hardlink boundary exceeded')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > limit or _digest(raw) != _sha(expected) or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('intake bytes changed or SHA mismatch')
    return raw, before.st_mtime_ns


def _bundle_path(stage, name):
    if not isinstance(name, str) or '\\' in name or ':' in name:
        raise ValueError('portable bundle path required')
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] != 'bundles':
        raise ValueError('bundle must remain within the reviewed staging bundles directory')
    return _path(stage / name)


def _references(value):
    stack = [value]
    visited = 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > 10000:
            raise ValueError('original reference graph exceeds bound')
        if isinstance(node, dict):
            if 'path' in node and 'sha256' in node:
                if not isinstance(node['path'], str):
                    raise ValueError('original reference path required')
                yield node
            stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (list, dict)))


def plan_intake(stage_root, manifest_sha256, runtime_root, *, now=None):
    stage, runtime = _path(stage_root), _path(runtime_root)
    if not stage.is_dir() or not runtime.is_dir():
        raise ValueError('explicit existing staging and runtime roots required')
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('aware intake clock required')
    day = current.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    raw, stamp = _read(stage / 'manifest.json', manifest_sha256, 100_000)
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get('schema') != 'valuation_daily_intake_v1'
            or manifest.get('trade_date') != day or not isinstance(manifest.get('reviewed_by'), str)
            or not manifest['reviewed_by'].strip()):
        raise ValueError('reviewed manifest for the actual current session required')
    reviewed = datetime.fromisoformat(manifest.get('reviewed_at', ''))
    if (reviewed.tzinfo is None or reviewed > current or
            reviewed.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() != day):
        raise ValueError('same-session nonfuture named review required')
    entries = manifest.get('bundles')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise ValueError('one to sixteen reviewed bundles required')
    bundles, objects, pending, seen, names = [], {}, [], set(), set()
    total = 0
    for entry in entries:
        h, name = _sha(entry['sha256']), entry['path']
        if h in seen or name in names:
            raise ValueError('duplicate bundle identity/path')
        seen.add(h)
        names.add(name)
        path = _bundle_path(stage, name)
        body, mtime = _read(path, h, 8_000_000)
        total += len(body)
        if total > 32_000_000:
            raise ValueError('daily bundle size bound exceeded')
        bundle = json.loads(body)
        reviews = bundle.get('reviews')
        if not isinstance(reviews, list) or not 1 <= len(reviews) <= 12000 or any(
                not isinstance(r, dict) or r.get('trade_date') != day for r in reviews):
            raise ValueError('bundle must contain reviews for this session')
        bundles.append({'relative_path': name, 'sha256': h, 'bytes': len(body), 'mtime_ns': mtime})
        pending.extend(_references(bundle))
    while pending:
        if len(objects) >= 2048:
            raise ValueError('original object count exceeds bound')
        reference = pending.pop()
        h = _sha(reference['sha256'])
        if h in objects:
            continue
        existing = _path(runtime / 'evidence/objects' / h)
        if existing.exists():
            source, kind = existing, 'existing_stable_object'
        else:
            source, kind = _path(stage / 'originals' / h), 'reviewed_staging_original'
            if not source.exists():
                supplied = Path(reference['path'])
                source = _path(supplied if supplied.is_absolute() else stage / supplied)
                if not source.is_relative_to(stage):
                    raise ValueError('new original may not be read outside reviewed staging')
        body, mtime = _read(source, h, MAX_ORIGINAL)
        if body.startswith(b'%PDF-'):
            pass
        else:
            parsed = json.loads(body)
            if not isinstance(parsed, (list, dict)):
                raise ValueError('only PDF or structured original JSON is admitted')
            pending.extend(_references(parsed))
        objects[h] = {'source': str(source), 'kind': kind, 'sha256': h,
                      'bytes': len(body), 'mtime_ns': mtime}
    if sum(v['bytes'] for v in objects.values()) > 256_000_000:
        raise ValueError('bounded admission batch required')
    materials = {'manifest_sha256': manifest_sha256, 'manifest_mtime_ns': stamp,
                 'bundles': [{k: v[k] for k in ('relative_path', 'sha256', 'bytes', 'mtime_ns')} for v in bundles],
                 'originals': sorted((h, v['bytes'], v['mtime_ns']) for h, v in objects.items())}
    return {'schema': 'valuation_intake_admission_plan_v1', 'stage_root': str(stage),
            'runtime_root': str(runtime), 'trade_date': day, 'manifest_sha256': manifest_sha256,
            'manifest_mtime_ns': stamp, 'material_fingerprint': _digest(_canonical(materials)),
            'bundles': bundles, 'objects': objects, 'network_requests': 0,
            'business_database_writes': 0, 'financial_qualification_granted': False}


def _publish_bytes(path, raw, mtime=None):
    """Atomic reserve-or-existing; a temporary hardlink never overwrites a peer."""
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _read(path, _digest(raw), len(raw))
        return False
    temporary = path.parent / ('.incoming-' + uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if mtime is not None:
            os.utime(temporary, ns=(mtime, mtime))
        try:
            os.link(temporary, path)
        except FileExistsError:
            _read(path, _digest(raw), len(raw))
            return False
        return True
    finally:
        if temporary.exists():
            temporary.unlink()  # Only this call's private unpublished name; never a guard.


def admit_intake(plan, expected_plan_sha256, runtime_root, *, now=None):
    runtime = _path(runtime_root)
    if (plan_sha256(plan) != _sha(expected_plan_sha256)
            or plan.get('schema') != 'valuation_intake_admission_plan_v1'
            or plan.get('runtime_root') != str(runtime)):
        raise ValueError('exact reviewed plan and independent runtime binding required')
    # Only control-directory/guard creation precedes ownership. Recheck source
    # materials under the owner so peers cannot observe an in-flight CAS link.
    intake = _path(runtime / 'workspace/valuation-intake')
    intake.mkdir(parents=True, exist_ok=True)
    guard = _path(intake / 'admission.guard')
    with FileLock(guard):
        fresh = plan_intake(plan['stage_root'], plan['manifest_sha256'], runtime, now=now)
        if fresh['material_fingerprint'] != plan['material_fingerprint']:
            raise ValueError('reviewed material set changed under owner')
        day = _path(intake / plan['trade_date'].replace('-', ''))
        intent = {'schema': 'valuation_intake_intent_v1', 'manifest_sha256': plan['manifest_sha256'],
                  'material_fingerprint': plan['material_fingerprint']}
        intent_raw = _canonical(intent)
        if day.exists():
            if not (day / 'admission-intent.json').is_file():
                raise ValueError('existing intake day has no matching owned intent; inspect without overwriting')
            _read(day / 'admission-intent.json', _digest(intent_raw), len(intent_raw))
        else:
            day.mkdir(exist_ok=False)
            _publish_bytes(day / 'admission-intent.json', intent_raw)
        allowed = {'admission-intent.json', 'admission-receipt.json', 'manifest.sha256', 'manifest.json',
                   *(v['relative_path'] for v in fresh['bundles'])}
        if any(p.relative_to(day).as_posix() not in allowed for p in day.rglob('*') if p.is_file()):
            raise ValueError('unrelated files in intake day; never overwrite or discard')
        for h, original in fresh['objects'].items():
            body, _ = _read(Path(original['source']), h, MAX_ORIGINAL)
            _publish_bytes(runtime / 'evidence/objects' / h, body, original['mtime_ns'])
        for bundle in fresh['bundles']:
            body, _ = _read(_bundle_path(Path(plan['stage_root']), bundle['relative_path']),
                            bundle['sha256'], 8_000_000)
            _publish_bytes(day / bundle['relative_path'], body, bundle['mtime_ns'])
        raw, _ = _read(Path(plan['stage_root']) / 'manifest.json', plan['manifest_sha256'], 100_000)
        existing_manifest = (day / 'manifest.json').exists()
        receipt = {'schema': 'valuation_intake_admission_v1', 'manifest_sha256': plan['manifest_sha256'],
                   'material_fingerprint': plan['material_fingerprint'], 'trade_date': plan['trade_date'],
                   'objects': len(fresh['objects']), 'bundles': len(fresh['bundles']),
                   'network_requests': 0, 'business_database_writes': 0,
                   'financial_qualification_granted': False, 'execution_ready': False}
        _publish_bytes(day / 'admission-receipt.json', _canonical(receipt))
        _publish_bytes(day / 'manifest.sha256', (plan['manifest_sha256'] + '\n').encode())
        _publish_bytes(day / 'manifest.json', raw, plan['manifest_mtime_ns'])
        return {**receipt, 'status': 'already_admitted' if existing_manifest else 'admitted_bytes_only'}
