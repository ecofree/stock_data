"""Legacy runs require a verified disposable copy, never the active checkout."""
import hashlib
import json
from pathlib import Path
import stat




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


def collection_source_files(root, *, strict_local_imports=False):
    """Bind collector sources without executing them.

    Strict identity checks require a present entrypoint and a closed set of
    local imports. The ordinary legacy collection contract retains its former
    discovery behavior until explicitly migrated by its caller.
    """
    import ast

    root = Path(root).resolve()
    pending = []
    runner = root / 'scripts/run_integrated_daily.py'
    if runner.is_file():
        pending.append(runner)
    elif strict_local_imports:
        raise ValueError('canonical collector entry missing')
    selected = set()
    trees = {}
    parsed_bytes = 0
    # These are project-owned names even after their file is removed from a
    # release. In particular, retired root shims must not become "third party"
    # merely because both the file and its manifest entry have disappeared.
    local_namespaces = {'trade_system', 'collectors', 'tools', 'scripts',
        'base', 'config', 'schema', 'fetch_all', 'setup',
        'collect_advanced', 'collect_advanced_stock', 'collect_daily',
        'collect_dingpan', 'collect_fengk', 'collect_finance', 'collect_index',
        'collect_l2', 'collect_ladder', 'collect_lhb', 'collect_market',
        'collect_misc', 'collect_news', 'collect_sector', 'collect_stock'}

    def tree(path):
        nonlocal parsed_bytes
        if path not in trees:
            size = path.stat().st_size
            parsed_bytes += size
            if len(trees) >= 10000 or size > 16_000_000 or parsed_bytes > 256_000_000:
                raise ValueError('collector source inspection budget exceeded')
            trees[path] = ast.parse(path.read_text(encoding='utf-8-sig'))
        return trees[path]

    def local_name(name):
        top = name.split('.')[0]
        return top in local_namespaces or (root/top).is_dir() or (root/top).with_suffix('.py').is_file()

    def check_local_path(path):
        if not strict_local_imports:
            return
        if root not in path.resolve().parents:
            raise ValueError('collector source member escapes root')
        for member in (path, *path.parents):
            info = member.lstat()
            if stat.S_ISLNK(info.st_mode) or (getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                raise ValueError('linked local collector import')

    def module_path(name, *, required=False):
        if required and (not name or any(not part.isidentifier() for part in name.split('.'))):
            raise ValueError('unresolved local collector import')
        path = root.joinpath(*name.split('.'))
        if required:
            for level in range(1,len(name.split('.'))):
                prefix=root.joinpath(*name.split('.')[:level])
                if prefix.with_suffix('.py').is_file() and not (prefix/'__init__.py').is_file():
                    raise ValueError('local collector package shadowed by module: ' + name)
        # Python chooses an initialized package before an equally named .py.
        candidates=(path / '__init__.py',path.with_suffix('.py')) if strict_local_imports else (
            path.with_suffix('.py'),path / '__init__.py')
        for candidate in candidates:
            if candidate.is_file():
                check_local_path(candidate)
                pending.append(candidate)
                return candidate
        if path.is_dir():  # PEP 420 namespaces do not need an __init__.py.
            check_local_path(path)
            return path
        if required:
            raise ValueError('missing local collector import: ' + name)
        return None

    def assigned_names(target):
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return set().union(*(assigned_names(item) for item in target.elts))
        return set()

    def declared_attribute(module, name):
        """Only explicit module bindings count; __getattr__/__all__ cannot prove one."""
        if module is None or module.is_dir():
            return False
        bound=False
        for node in tree(module).body:
            if isinstance(node, ast.Delete) and any(name in assigned_names(target) for target in node.targets):
                bound=False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                bound=True
            if isinstance(node, ast.Assign) and any(name in assigned_names(target) for target in node.targets):
                bound=True
            if isinstance(node, ast.AnnAssign) and node.value is not None and name in assigned_names(node.target):
                bound=True
            if isinstance(node, ast.Import) and any((item.asname or item.name.split('.')[0]) == name for item in node.names):
                bound=True
            if isinstance(node, ast.ImportFrom) and any(item.name != '*' and (item.asname or item.name) == name for item in node.names):
                bound=True
        return bound

    def interpreter(node):
        if isinstance(node, ast.Name):
            return node.id in {'py','python','python_executable','python_path'}
        if isinstance(node, ast.Attribute):
            return isinstance(node.value,ast.Name) and node.value.id=='sys' and node.attr=='executable'
        if isinstance(node, ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='str' and len(node.args)==1:
            return interpreter(node.args[0])
        if isinstance(node, ast.Constant) and isinstance(node.value,str):
            filename=node.value.replace('\\','/').rsplit('/',1)[-1].lower()
            return filename in {'python','python3','python.exe','python3.exe','py','py.exe'}
        return False

    def command_sources(node, *, explicit_subprocess=False):
        # Bind explicit command vectors, including vectors inside phase plans.
        # A prose string merely mentioning a removed .py file is not an entry.
        if explicit_subprocess and isinstance(node,ast.Constant) and isinstance(node.value,str):
            import shlex
            parts=[item.strip('\"\'') for item in shlex.split(node.value,posix=False)]
            if parts and (interpreter(ast.Constant(value=parts[0])) or parts[0].endswith('.py')):
                node=ast.List(elts=[ast.Constant(value=item) for item in parts])
        if not isinstance(node,(ast.List,ast.Tuple)) or not node.elts or not (
                explicit_subprocess or interpreter(node.elts[0])):
            return
        for index,item in enumerate(node.elts):
            if not isinstance(item,ast.Constant) or not isinstance(item.value,str):
                continue
            if item.value=='-m' and index+1<len(node.elts):
                module=node.elts[index+1]
                if isinstance(module,ast.Constant) and isinstance(module.value,str):
                    module_path(module.value,required=local_name(module.value))
            name=item.value.replace('\\','/')
            if not name.endswith('.py'):
                continue
            path=root/name
            if not path.is_file():
                raise ValueError('missing local collector command source: ' + name)
            check_local_path(path)
            pending.append(path)

    while pending:
        path = pending.pop()
        if path in selected:
            continue
        check_local_path(path)
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
        nodes=list(ast.walk(tree(path)))
        subprocess_names={'subprocess'}
        subprocess_calls=set()
        for node in nodes:
            if isinstance(node,ast.Import):
                subprocess_names.update(item.asname or item.name for item in node.names if item.name=='subprocess')
            elif isinstance(node,ast.ImportFrom) and node.module=='subprocess' and not node.level:
                subprocess_calls.update(item.asname or item.name for item in node.names
                    if item.name in {'run','Popen','call','check_call','check_output'})
        for node in nodes:
            if strict_local_imports:
                command_sources(node)
                if isinstance(node,ast.Call) and (
                    isinstance(node.func,ast.Name) and node.func.id in subprocess_calls
                    or isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name)
                    and node.func.value.id in subprocess_names
                    and node.func.attr in {'run','Popen','call','check_call','check_output'}
                ):
                    for command in node.args[:1]+[item.value for item in node.keywords if item.arg=='args']:
                        command_sources(command,explicit_subprocess=True)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_path(alias.name, required=strict_local_imports and local_name(alias.name))
            elif isinstance(node, ast.ImportFrom):
                if strict_local_imports and node.level > len(package):
                    raise ValueError('relative collector import escapes source package')
                prefix = package[:len(package) - node.level + 1] if node.level else []
                name = '.'.join(prefix + ([node.module] if node.module else []))
                required = strict_local_imports and (bool(node.level) or local_name(name))
                module = module_path(name, required=required)
                for alias in node.names:
                    if alias.name == '*':
                        if required:
                            raise ValueError('unresolved local wildcard collector import')
                    elif module_path('.'.join(filter(None, (name, alias.name)))) is None and required:
                        if not declared_attribute(module, alias.name):
                            raise ValueError('unresolved local collector attribute: ' + name + '.' + alias.name)
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
                if strict_local_imports and local_name(node.args[0].value) and (
                        len(node.args)>1 or node.keywords):
                    raise ValueError('unresolved dynamic local collector import arguments')
                module_path(node.args[0].value, required=strict_local_imports and local_name(node.args[0].value))
    # Configuration and migrations stay sealed; runtime caches and research outputs do not.
    for folder in ('migrations', 'config'):
        selected.update(p for p in (root / folder).rglob('*') if p.is_file()
                        and p.suffix in ('.sql', '.json', '.toml', '.yaml', '.yml'))
    selected.update(p for p in root.glob('requirements*.lock') if p.is_file())
    selected.update(p for p in (root / 'pyproject.toml', root / 'setup.py',
                                 root / 'source-distribution.json') if p.is_file())
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
    from trade_system.config import configured_runtime_root
    layout = configured_runtime_root()
    if layout is not None and (database != layout/'market/kpl_data.duckdb'
                               or reports != layout/'collection-reports'):
        raise ValueError('collector runtime layout database/report target mismatch')
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
    if not (root / 'scripts/run_integrated_daily.py').is_file():
        raise ValueError('canonical collector entry missing')
    files = collection_source_files(root)
    if any(p.is_symlink() or root not in p.resolve().parents for p in files):
        raise ValueError('collector source member escapes root')
    digests = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    runtime = subprocess.run([str(python), '-I', '-B', '-c',
        'import importlib.metadata as m,json,sys; print(json.dumps([list(sys.version_info[:3]),sorted((d.metadata["Name"],d.version) for d in m.distributions())]))'],
        capture_output=True, text=True, timeout=30, check=True)
    from trade_system.pipeline_runtime import default_observation_policy
    return {**({'runtime_root': str(layout)} if layout is not None else {}),
        'schema': 1, 'scope': 'transitional_market_collection_only', 'source_root': str(root),
        'database': str(database), 'reports': str(reports), 'python': str(python),
        'python_sha256': hashlib.sha256(python.read_bytes()).hexdigest(),
        'runtime_sha256': hashlib.sha256(json.dumps(json.loads(runtime.stdout), sort_keys=True).encode()).hexdigest(),
        'files': digests, 'observation_windows': default_observation_policy(),
        'execution_ready': False, 'production_cutover': False}

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
