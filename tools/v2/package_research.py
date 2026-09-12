"""Build a hash-bound, relocatable research source distribution (not the core wheel)."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[2]
ENTRY = 'trade_system.v2.research_product'
LAUNCHER = '''"""Verify the research release before importing application code."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
manifest = json.loads((root / 'research-release.json').read_text(encoding='utf-8'))
expected = manifest['files']
actual_python = {p.relative_to(root).as_posix() for p in root.rglob('*.py')}
if actual_python != {p for p in expected if p.endswith('.py')}:
    raise SystemExit('Research release contains missing or unexpected Python modules')
for relative, digest in expected.items():
    path = root / relative
    if path.is_symlink() or root not in path.resolve().parents or not path.is_file():
        raise SystemExit('Invalid research release member: ' + relative)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit('Changed research release member: ' + relative)
sys.path.insert(0, str(root))
from trade_system.v2.research_product import main
main()
'''


def source_closure(root=ROOT):
    """Static local imports, including function-local imports; reject legacy roots."""
    pending, selected = [ENTRY], {}
    while pending:
        module = pending.pop()
        path = root.joinpath(*module.split('.')).with_suffix('.py')
        if not path.is_file():
            path = root.joinpath(*module.split('.'), '__init__.py')
        if not path.is_file() or module in selected:
            continue
        if module in {'base', 'config', 'trade_system.tushare_relay'}:
            raise ValueError('research release depends on legacy collector authority: ' + module)
        selected[module] = path
        package = module if path.name == '__init__.py' else module.rpartition('.')[0]
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                parts = package.split('.')
                base = '.'.join(parts[:len(parts)-node.level+1]) if node.level else ''
                target = '.'.join(p for p in (base, node.module) if p)
                pending.append(target)
                pending.extend(target+'.'+a.name for a in node.names if a.name != '*')
    files = {p.relative_to(root).as_posix():p.read_bytes() for p in selected.values()}
    for name in list(files):
        parent = Path(name).parent
        while parent != Path('.'):
            initializer = parent/'__init__.py'
            files.setdefault(initializer.as_posix(), (root/initializer).read_bytes() if (root/initializer).is_file() else b'')
            parent = parent.parent
    return files


def build(output, root=ROOT):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('new research distribution path required')
    files = source_closure(root)
    for name in ('requirements-research-replay.lock','config/research_delivery.json'):
        files[name] = (root/name).read_bytes()
    files['run_research.py'] = LAUNCHER.encode('utf-8')
    files['README.txt'] = (
        'Research release, separate from operational core. No broker commands.\n'
        'Use a dedicated environment: python -m pip install --require-hashes -r requirements-research-replay.lock\n'
        'Existing frozen model replay is verified ONLY on Windows CPython 3.12.\n'
        'Then run: python -m pip check. Keep experiment runtime validation enabled.\n'
        'This single profile matches the old frozen model; no install-then-downgrade/uninstall sequence is required.\n'
        'Other platforms, Python versions and frozen runtimes require their own verified dependency profile.\n'
        'Run: python -I run_research.py preflight --config <frozen configuration>\n'
        'Run: python -I run_research.py status --output <existing research output>\n'
        'Run: python -I run_research.py serve --output <existing research output> --port 8768\n'
        'Data, model artifacts, credentials and operator facts are NOT included.\n'
        'Supply explicit external data paths/configuration; do not overwrite frozen evidence.\n'
        'Hashes detect accidental changes, not an attacker able to replace this release and its manifest.\n'
    ).encode('utf-8')
    manifest = {'schema':1, 'entry':ENTRY,
        'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'files':{n:hashlib.sha256(v).hexdigest() for n,v in sorted(files.items())},
        'scope':'isolated_research_source_distribution_not_operational_core',
        'execution_ready':False,'production_cutover':False}
    files['research-release.json'] = json.dumps(manifest,sort_keys=True,indent=2).encode('utf-8')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return {'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
            'files':len(files), 'scope':manifest['scope']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    print(json.dumps(build(parser.parse_args().output)))
