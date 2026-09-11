"""Legacy runs require a verified disposable copy, never the active checkout."""
import hashlib
import json
from pathlib import Path


def require_signal_copy(root, db):
    if root is None:
        raise ValueError('legacy signal writes retired; an explicit verified disposable migration copy is required')
    return require_copy(root, db, Path(root) / 'diagnostic-reports')


def require_copy(root,db,reports):
    root=Path(root).resolve(strict=True)
    db=Path(db).resolve(strict=True)
    reports=Path(reports).resolve()
    manifest=json.loads((root/'verification.json').read_text(encoding='utf-8'))
    if root not in db.parents or root not in reports.parents or reports==db:
        raise ValueError('legacy migration database and reports must remain inside verified copy directory')
    if not manifest.get('restore_verified') or Path(manifest['backup']).resolve()!=db:
        raise ValueError('verified backup manifest must bind the exact disposable database')
    source=Path(manifest['source']).resolve()
    if source==db or (source.exists() and source.samefile(db)):
        raise ValueError('migration copy must not alias its source')
    with db.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=manifest['sha256']:
            raise ValueError('migration copy changed since verification; use a new verified copy')
    if (reports/'current.json').exists() or (reports/'v2-publication-owner.json').exists():
        raise ValueError('legacy diagnostics cannot publish into the V2 namespace')
    return {'scope':'one_shot_disposable_legacy_migration_not_production','source':str(source),
            'database':str(db),'reports':str(reports)}
