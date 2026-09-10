"""Immutable artifact bundles and one atomically switched, verified pointer."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid

from trade_system.file_lock import FileLock
from .domain import canonical


def safe_child(root, name):
    name = PurePosixPath(name)
    if name.is_absolute() or '..' in name.parts or '\\' in str(name) or ':' in str(name):
        raise ValueError('unsafe artifact path')
    path = (root / str(name)).resolve()
    if root.resolve() not in path.parents:
        raise ValueError('artifact must stay within bundle')
    return path


def publish(root, run_id, artifacts, *, generation):
    root = Path(root).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id) or type(generation) is not int or generation < 0:
        raise ValueError('invalid run identity or generation')
    if not artifacts or 'manifest.json' in artifacts:
        raise ValueError('nonempty explicit artifact list required; manifest name is reserved')
    run = root / 'runs' / run_id
    run.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, content in artifacts.items():
        if not isinstance(content, bytes):
            raise ValueError('artifact content must be bytes')
        file = safe_child(run, name)
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open('xb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        hashes[name] = hashlib.sha256(content).hexdigest()
    manifest = {'run_id': run_id, 'generation': generation, 'artifacts': hashes}
    manifest_bytes = canonical(manifest).encode()
    with (run / 'manifest.json').open('xb') as handle:
        handle.write(manifest_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    pointer = {'run_id': run_id, 'generation': generation,
               'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()}
    with FileLock(root / 'publish.guard'):
        current = root / 'current.json'
        if current.exists() and json.loads(current.read_text(encoding='utf-8'))['generation'] >= generation:
            raise ValueError('older or equal generation cannot replace current bundle')
        temp = root / ('.current-' + uuid.uuid4().hex)
        with temp.open('xb') as handle:
            handle.write(canonical(pointer).encode())
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(current)
    return pointer


def read_current(root):
    root = Path(root).resolve()
    pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', pointer['run_id']):
        raise ValueError('invalid published run identity')
    run = safe_child(root, 'runs/' + pointer['run_id'])
    raw = (run / 'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != pointer['manifest_sha256']:
        raise ValueError('manifest checksum mismatch')
    manifest = json.loads(raw)
    if manifest['run_id'] != pointer['run_id'] or manifest['generation'] != pointer['generation']:
        raise ValueError('pointer/bundle identity mismatch')
    files = {}
    for name, expected in manifest['artifacts'].items():
        raw = safe_child(run, name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f'artifact checksum mismatch: {name}')
        files[name] = raw
    return manifest, files
