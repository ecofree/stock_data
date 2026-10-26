"""Explicit new rehearsal workspaces. No production defaults or execution hooks."""
import json
import os
import stat
from pathlib import Path, PurePosixPath
import uuid

from .domain import canonical, identity

SCOPE = 'stockdata_isolated_capabilities_v1'


def safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        reparse = part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)() or reparse:
            raise ValueError('links and junctions are forbidden in rehearsal paths')
    return path.resolve()


def initialize(path):
    path = safe_path(path)
    path.mkdir(parents=True, exist_ok=False)
    write_json(path / 'rehearsal.json', {'scope': SCOPE, 'execution_ready': False})
    return path


def workspace(path):
    path = safe_path(path)
    if read_json(path / 'rehearsal.json') != {'scope': SCOPE, 'execution_ready': False}:
        raise ValueError('explicit isolated rehearsal workspace required')
    return path


def child(root, name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('portable relative rehearsal path required')
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('unsafe rehearsal path')
    if any(p.endswith((' ', '.')) or Path(p).is_reserved() for p in relative.parts):
        raise ValueError('ambiguous Windows path component')
    path = safe_path(Path(root) / name)
    if not path.is_relative_to(safe_path(root)) or path == safe_path(root):
        raise ValueError('path must stay below rehearsal root')
    return path


def read_json(path):
    path = safe_path(path)
    if path.stat().st_size > 16_000_000:
        raise ValueError('bounded JSON input required')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(path.read_text('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def write_json(path, value):
    """Flush before atomic, no-overwrite publication. Failed staging is retained."""
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    staging = path.with_name('.pending-' + path.name + '-' + uuid.uuid4().hex)
    with staging.open('x', encoding='utf-8') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    # link fails if the final name already exists; unlike replace, it cannot
    # overwrite an immutable receipt. Both names are on the same filesystem.
    os.link(staging, path)
    staging.unlink()


def seal(value):
    return {**value, 'receipt_id': identity(value)}


def checked(value):
    if not isinstance(value, dict) or value.get('receipt_id') != identity({k: v for k, v in value.items() if k != 'receipt_id'}):
        raise ValueError('receipt identity mismatch')
    return value


def new_receipt(root, category, value):
    receipt = seal(value)
    path = child(root, category + '/' + uuid.uuid4().hex + '.json')
    write_json(path, receipt)
    return receipt
