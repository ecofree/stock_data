"""Bounded read-only access to original evidence, independent of checkout paths.

With an explicit runtime root, the content-addressed store is authoritative:
missing or changed objects fail closed, even if the former path still exists.
Without a deployed layout, existing explicitly supplied local originals remain
usable by development and historical import tools. No search, fetch or write.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from trade_system.config import configured_runtime_root


def read_original(reference: dict, *, max_bytes: int = 32 * 1024 * 1024) -> bytes:
    digest = reference.get('sha256')
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('original evidence exact SHA256 required')
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('positive original evidence read budget required')
    root = configured_runtime_root()
    if root is not None:
        path = root / 'evidence' / 'objects' / digest
    else:
        if not isinstance(reference.get('path'), str) or not reference['path']:
            raise ValueError('explicit original evidence path required')
        path = Path(reference['path'])
    for member in (path, *path.parents):
        if member.is_symlink() or getattr(member, 'is_junction', lambda: False)():
            raise ValueError('original evidence must not traverse a link or junction')
    # Read at most limit+1 even if a file changes between stat and open.
    if path.stat().st_size > max_bytes:
        raise ValueError('original evidence exceeds size limit')
    with path.open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('original evidence exceeds size limit')
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('original evidence document changed: hash mismatch')
    return raw
