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


def collection_contract(source_root, database, reports, python):
    """Seal existing market collectors for an explicit transitional task adapter.

    This is not an authorization token or a bypass of legacy writer guards.
    The operator still approves the task actions and protected release directory.
    No script, database, credentials, account or scheduler state is modified.
    """
    import hashlib
    import json
    from pathlib import Path
    import subprocess
    import duckdb

    inputs = [Path(p).absolute() for p in (source_root, database, reports, python)]
    for path in inputs:
        if any(p.is_symlink() or (p.exists() and getattr(p, 'is_junction', lambda: False)()) for p in (path, *path.parents)):
            raise ValueError('collector paths must not traverse links or junctions')
    root, database, reports, python = (p.resolve() for p in inputs)
    if not root.is_dir() or not python.is_file() or not database.is_file():
        raise ValueError('existing collector source, interpreter and market database required')
    if reports == root or reports == database.parent or database in reports.parents:
        raise ValueError('collection diagnostics require a separate output directory')
    with duckdb.connect(str(database), read_only=True) as con:
        names = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
        if names & {'v2_schema', 'account_snapshot', 'reservation', 'paper_ledger_event'}:
            raise ValueError('transitional collection refuses V2/account databases')
        if not {'tushare_trade_cal', 'v_kline_daily'} <= names:
            raise ValueError('initialized market database and calendar required')
    files = set(root.glob('*.py')) | set(root.glob('requirements*.lock'))
    # These are collector limiter/cache state and historical research outputs,
    # not executable source/configuration. Their normal updates must not revoke
    # the next collection run. Research evidence has its own immutable binding.
    runtime_outputs = ('trade_system/.stock_cache/', 'research/phase_abc/reports/',
                       'research/phase_abc/snapshot/')
    for folder in ('scripts', 'trade_system', 'collectors', 'research', 'migrations', 'config'):
        files.update(p for p in (root / folder).rglob('*') if p.is_file()
            and p.suffix in ('.py', '.sql', '.json', '.toml', '.yaml', '.yml')
            and not p.relative_to(root).as_posix().startswith(runtime_outputs))
    if not (root / 'fetch_all.py').is_file():
        raise ValueError('canonical collector entry missing')
    if any(p.is_symlink() or root not in p.resolve().parents for p in files):
        raise ValueError('collector source member escapes root')
    digests = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    runtime = subprocess.run([str(python), '-I', '-c',
        'import importlib.metadata as m,json,sys; print(json.dumps([list(sys.version_info[:3]),sorted((d.metadata["Name"],d.version) for d in m.distributions())]))'],
        capture_output=True, text=True, timeout=30, check=True)
    return {'schema': 1, 'scope': 'transitional_market_collection_only', 'source_root': str(root),
        'database': str(database), 'reports': str(reports), 'python': str(python),
        'python_sha256': hashlib.sha256(python.read_bytes()).hexdigest(),
        'runtime_sha256': hashlib.sha256(json.dumps(json.loads(runtime.stdout), sort_keys=True).encode()).hexdigest(),
        'files': digests, 'execution_ready': False, 'production_cutover': False}

def verify_collection_contract(path, expected_sha256, database, reports):
    import hashlib
    import json
    import re
    from pathlib import Path

    raw = Path(path).read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError('collector contract exceeds bounded manifest size')
    if not re.fullmatch('[0-9a-fA-F]{64}', expected_sha256 or '') or hashlib.sha256(raw).hexdigest() != expected_sha256.lower():
        raise ValueError('collector contract differs from the explicitly supplied hash')
    saved = json.loads(raw)
    if saved.get('scope') != 'transitional_market_collection_only' or saved.get('execution_ready') is not False:
        raise ValueError('unknown collector handover scope')
    if Path(database).resolve() != Path(saved['database']) or Path(reports).resolve() != Path(saved['reports']):
        raise ValueError('collector handover database/output target changed')
    if collection_contract(saved['source_root'], database, reports, saved['python']) != saved:
        raise ValueError('collector source/runtime changed; prepare and approve a new handover')
    return saved
