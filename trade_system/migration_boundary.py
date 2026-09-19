"""Legacy runs require a verified disposable copy, never the active checkout."""
import hashlib
import json
from pathlib import Path




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


def collection_source_files(root):
    """Bind transitive collector imports and literal script entries without importing them."""
    import ast

    root = Path(root).resolve()
    pending = [root / 'fetch_all.py']
    runner = root / 'scripts/run_integrated_daily.py'
    if runner.is_file():
        pending.append(runner)
    selected = set()

    def module_path(name):
        path = root.joinpath(*name.split('.'))
        for candidate in (path.with_suffix('.py'), path / '__init__.py'):
            if candidate.is_file():
                pending.append(candidate)
                return

    while pending:
        path = pending.pop()
        if path in selected:
            continue
        if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
               for p in (path, *path.parents)) or root not in path.resolve().parents:
            raise ValueError('collector source member escapes root')
        selected.add(path)
        relative = path.relative_to(root)
        package = list(relative.parent.parts)
        # Package initializers execute too, even when only a submodule is imported.
        for n in range(1, len(package) + 1):
            initializer = root.joinpath(*package[:n], '__init__.py')
            if initializer.is_file():
                pending.append(initializer)
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_path(alias.name)
            elif isinstance(node, ast.ImportFrom):
                prefix = package[:len(package) - node.level + 1] if node.level else []
                name = '.'.join(prefix + ([node.module] if node.module else []))
                module_path(name)
                for alias in node.names:
                    if alias.name != '*':
                        module_path('.'.join(filter(None, (name, alias.name))))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Subprocess entries are source dependencies even without an import.
                name = node.value.replace('\\', '/')
                if name.endswith('.py') and (root / name).is_file():
                    pending.append(root / name)
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == '__import__'
                or isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module'
            ):
                if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                    raise ValueError('unresolved dynamic collector import')
                module_path(node.args[0].value)
    # Configuration and migrations stay sealed; runtime caches and research outputs do not.
    for folder in ('migrations', 'config'):
        selected.update(p for p in (root / folder).rglob('*') if p.is_file()
                        and p.suffix in ('.sql', '.json', '.toml', '.yaml', '.yml'))
    selected.update(p for p in (root / 'requirements.lock', root / 'requirements-build.lock') if p.is_file())
    return selected


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
    if not (root / 'fetch_all.py').is_file():
        raise ValueError('canonical collector entry missing')
    files = collection_source_files(root)
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
