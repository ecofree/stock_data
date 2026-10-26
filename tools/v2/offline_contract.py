"""Stdlib-only material contract shared by sealing, preflight and rebuilding.

Static admission proves pinned Windows CPython 3.12 materials are present.
Actual environment rebuild/imports and business verification remain separate gates.
"""
import ast
from email.parser import Parser
import hashlib
from pathlib import Path, PurePosixPath
import re
import zipfile

LOCKS = ('requirements-dev.lock', 'requirements-data.lock', 'requirements-build.lock')
BUILD_SCRIPTS = ('tools/v2/prepare_development_runtime.py', 'tools/v2/offline_contract.py',
                 'tools/v2/offline_process.py')
ENTRYPOINTS = (*BUILD_SCRIPTS, 'tools/v2/offline_package.py',
               'tools/v2/isolated_capabilities.py', 'tools/v2/verify_offline_recovery.py')
CORE_PACKAGES = {'pytest', 'ruff', 'duckdb', 'pandas', 'pyarrow', 'pypdf', 'setuptools', 'jsonpath'}


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def locked_blocks(path):
    result = {}; name, lines = None, []
    for line in Path(path).read_text('utf-8').splitlines():
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', line)
        if match:
            if name: result[name] = '\n'.join(lines) + '\n'
            name, lines = normalized(match[1]), [line]
        elif name and (line.startswith(' ') or not line.strip()):
            lines.append(line)
        elif name:
            result[name] = '\n'.join(lines) + '\n'; name, lines = None, []
    if name: result[name] = '\n'.join(lines) + '\n'
    return result


def runtime_blocks(code, jsonpath_sha):
    blocks = {}
    for name in LOCKS: blocks.update(locked_blocks(Path(code) / name))
    if not CORE_PACKAGES <= blocks.keys():
        raise ValueError('incomplete development lock closure')
    original = blocks['jsonpath']
    blocks['jsonpath'] = 'jsonpath==0.82.2 --hash=sha256:' + jsonpath_sha + '\n'
    return blocks, original


def base_entries(archive):
    """The exact clean bootstrap/base subset; never installed site-packages."""
    seen = set(); total = 0
    for item in archive.infolist():
        parts = PurePosixPath(item.filename.replace('\\', '/')).parts
        if not parts or parts[0] != 'python-collector' or '..' in parts:
            raise ValueError('unexpected base archive namespace')
        if item.is_dir() or len(parts) < 2: continue
        relative = PurePosixPath(*parts[1:]); name = relative.as_posix()
        if relative.is_absolute() or ':' in name or (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError('linked/unsafe original member')
        if (name.startswith(('Lib/site-packages/', 'Scripts/')) or '__pycache__' in relative.parts
                or relative.suffix in ('.pyc', '.pyo')
                or name in ('pyvenv.cfg', 'runtime-manifest.json', 'Lib/EXTERNALLY-MANAGED')):
            continue
        total += item.file_size
        if name.casefold() in seen or total > 512*1024**2 or len(seen) >= 10000:
            raise ValueError('duplicate or excessive base material')
        seen.add(name.casefold())
        yield name, archive.read(item)


def code_closure(code):
    """Follow local imports without executing package code or optional services."""
    pending = list(ENTRYPOINTS) + ['trade_system/__init__.py', 'trade_system/v2/__init__.py']
    visited = set()

    def require(module):
        path = code.joinpath(*module.split('.'))
        if path.with_suffix('.py').is_file(): candidate = path.with_suffix('.py')
        elif (path / '__init__.py').is_file(): candidate = path / '__init__.py'
        elif module in ('tools', 'tools.v2') and path.is_dir(): return True
        else: raise ValueError('missing local recovery module: ' + module)
        pending.append(candidate.relative_to(code).as_posix())
        for parent in candidate.parents:
            if parent == code: break
            if (parent / '__init__.py').is_file():
                pending.append((parent / '__init__.py').relative_to(code).as_posix())
        return candidate.name == '__init__.py'

    while pending:
        name = pending.pop()
        if name in visited: continue
        path = code / name
        if not path.is_file() or path.stat().st_size > 2_000_000 or len(visited) >= 3000:
            raise ValueError('missing or excessive recovery code: ' + name)
        visited.add(name)
        tree = ast.parse(path.read_text('utf-8-sig'), filename=name)
        package = list(PurePosixPath(name).parts[:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] in ('trade_system', 'tools'): require(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package): raise ValueError('relative recovery import escapes code')
                    base = '.'.join(package[:len(package)-node.level+1])
                    module = base + ('.' + node.module if node.module else '')
                else: module = node.module or ''
                if module.split('.')[0] not in ('trade_system', 'tools'): continue
                is_package = require(module)
                if is_package:
                    for alias in node.names:
                        if alias.name != '*': require(module + '.' + alias.name)
    return sorted(visited)


def wheel_identity(path):
    parts = path.name[:-4].split('-')
    if path.suffix != '.whl' or len(parts) not in (5, 6):
        raise ValueError('invalid offline wheel filename')
    name, version = normalized(parts[0]), parts[1]
    python, abi, platform = parts[-3:]
    tags = {p+'-'+a+'-'+s for p in python.split('.') for a in abi.split('.') for s in platform.split('.')}
    compatible = any(
        (s == 'any' and a == 'none' and p in ('py3', 'py312')) or
        (s == 'win_amd64' and ((p == 'cp312' and a in ('cp312', 'abi3', 'none')) or
                             (p in ('py3', 'py312') and a == 'none') or
                             (re.fullmatch(r'cp3\d+', p) and int(p[3:]) <= 12 and a == 'abi3')))
        for p, a, s in (t.split('-') for t in tags))
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        # Vendored dependencies may carry nested dist-info directories. Only
        # root-level dist-info belongs to this wheel's installed distribution.
        metadata = [n for n in names if n.count('/') == 1 and n.endswith('.dist-info/METADATA')]
        wheel = [n for n in names if n.count('/') == 1 and n.endswith('.dist-info/WHEEL')]
        if len(metadata) != 1 or len(wheel) != 1 or metadata[0].rsplit('/',1)[0] != wheel[0].rsplit('/',1)[0]:
            raise ValueError('ambiguous offline wheel metadata')
        if any(archive.getinfo(n).file_size > 1_000_000 for n in (metadata[0], wheel[0])):
            raise ValueError('offline wheel metadata budget exceeded')
        meta = Parser().parsestr(archive.read(metadata[0]).decode('utf-8'))
        wheel_meta = Parser().parsestr(archive.read(wheel[0]).decode('utf-8'))
        if normalized(meta.get('Name','')) != name or meta.get('Version') != version or set(wheel_meta.get_all('Tag',[])) != tags:
            raise ValueError('wheel filename and embedded identity differ')
    return name, version, compatible


def validate_closure(root, files, *, base_sha, jsonpath_name, jsonpath_sha, catalog, member):
    required = ('RUN-RESTORE.ps1', 'README-离机验收.txt', 'environment/base.zip',
                'environment/wheels/' + jsonpath_name, 'rehearsal/rehearsal.json',
                'rehearsal/bundles/initial/completed.json', 'rehearsal/' + catalog + '/completed.json',
                *('code/' + n for n in (*LOCKS, *ENTRYPOINTS)))
    if any(n not in files for n in required): raise ValueError('incomplete offline execution materials')
    for name in required: member(root, name)
    if files['environment/base.zip']['sha256'] != base_sha or files['environment/wheels/'+jsonpath_name]['sha256'] != jsonpath_sha:
        raise ValueError('offline runtime identity differs')
    with zipfile.ZipFile(root/'environment/base.zip') as archive:
        expected = {name:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in base_entries(archive)}
    if not {'python.exe','python312.dll','Lib/encodings/__init__.py'} <= expected.keys():
        raise ValueError('incomplete standalone Python base')
    if {n.removeprefix('bootstrap/'):v for n,v in files.items() if n.startswith('bootstrap/')} != expected:
        raise ValueError('bootstrap differs from complete pinned base')
    code = code_closure(root/'code')
    if any('code/'+n not in files for n in code): raise ValueError('recovery code is not in manifest')
    blocks, _ = runtime_blocks(root/'code',jsonpath_sha)
    candidates = {}
    for name, record in files.items():
        if name.startswith('environment/wheels/') and name.endswith('.whl'):
            package, version, compatible = wheel_identity(member(root,name))
            if compatible: candidates.setdefault((package,version),[]).append((name,record['sha256']))
    selected = {}
    for name, block in blocks.items():
        version = re.match(r'^[A-Za-z0-9_.-]+==([^\s\\]+)',block)[1]
        hashes = set(re.findall(r'--hash=sha256:([0-9a-f]{64})\b',block))
        matches = sorted((p,h) for p,h in candidates.get((name,version),[]) if h in hashes)
        if not matches: raise ValueError('missing pinned compatible wheel: '+name+'=='+version)
        selected[name] = {'version':version,'file':matches[0][0],'sha256':matches[0][1]}
    return {'schema':'offline_material_closure_v1','target':'CPython-3.12-Windows-x64',
            'code':code,'wheels':selected,'bootstrap_files':len(expected),
            'rebuild_and_business_checks_still_required':True}
