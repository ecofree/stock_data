"""Resolve one immutable feature export using an atomic current pointer."""
import hashlib
import json
from pathlib import Path


def resolve_feature_path(path):
    path = Path(path).resolve()
    pointer = path.with_suffix('.current.json')
    if not pointer.exists():
        return path  # explicitly supported legacy read, not a V2 certificate
    data = json.loads(pointer.read_text(encoding='utf-8'))
    root = path.parent
    target = (root / data['outputs'][path.suffix.lstrip('.')]).resolve()
    if root not in target.parents:
        raise ValueError('feature pointer escapes export root')
    metadata = target.with_suffix('.metadata.json')
    if hashlib.sha256(metadata.read_bytes()).hexdigest() != data['metadata_sha256']:
        raise ValueError('feature metadata checksum mismatch')
    meta = json.loads(metadata.read_text(encoding='utf-8'))
    for relative, expected in meta['artifact_hashes'].items():
        file = (metadata.parent / relative).resolve()
        if metadata.parent not in file.parents or not file.is_file():
            raise ValueError('invalid feature artifact reference')
        digest = hashlib.sha256()
        with file.open('rb') as handle:
            for block in iter(lambda: handle.read(8*1024*1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError('feature artifact checksum mismatch')
    return target
