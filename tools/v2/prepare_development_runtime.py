"""Prepare one independent development interpreter from retained, hash-pinned offline artifacts.

Only creates ROOT/.venv and a fresh evidence directory. Never reads provider
credentials, copies installed site-packages, changes a production runtime, or
modifies global Python/PATH. No network resolution or package upgrades.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def locked_blocks(path):
    result = {}
    name, lines = None, []
    for line in Path(path).read_text('utf-8').splitlines():
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', line)
        if match:
            if name:
                result[name] = '\n'.join(lines) + '\n'
            name = re.sub(r'[-_.]+', '-', match[1]).lower()
            lines = [line]
        elif name and (line.startswith(' ') or not line.strip()):
            lines.append(line)
        elif name:
            result[name] = '\n'.join(lines) + '\n'
            name, lines = None, []
    if name:
        result[name] = '\n'.join(lines) + '\n'
    return result


def prepare(base_archive, base_sha, wheelhouse, jsonpath_wheel, jsonpath_sha, output):
    target = ROOT / '.venv'
    for path in (target, output, base_archive, wheelhouse, jsonpath_wheel):
        for member in (path, *path.parents):
            if member.is_symlink() or member.is_junction():
                raise ValueError('linked input/output rejected')
    if target.exists() or output.exists():
        raise ValueError('new development runtime and evidence paths required; inspect rather than overwrite')
    if digest(base_archive) != base_sha or digest(jsonpath_wheel) != jsonpath_sha:
        raise ValueError('exact retained archive/wheel SHA required')
    locks = ('requirements-dev.lock', 'requirements-data.lock', 'requirements-build.lock')
    blocks = {}
    for name in locks:
        blocks.update(locked_blocks(ROOT / name))
    if not {'pytest', 'ruff', 'duckdb', 'pyarrow', 'pypdf', 'setuptools', 'jsonpath'} <= blocks.keys():
        raise ValueError('incomplete development lock closure')
    original_jsonpath = blocks['jsonpath']
    # This wheel was built offline from the original pinned sdist; the caller
    # supplies its independently retained build digest, never a guessed wheel.
    blocks['jsonpath'] = 'jsonpath==0.82.2 --hash=sha256:' + jsonpath_sha + '\n'
    output.mkdir(parents=True, exist_ok=False)
    lock = output / 'development-runtime.lock'
    lock.write_text('# Derived only from the reviewed repository locks and pinned local jsonpath build.\n' +
                    ''.join(blocks[n] for n in sorted(blocks)), encoding='utf-8')
    environment = {k: v for k, v in os.environ.items() if not k.startswith(
        ('GIT_', 'KPL_', 'TUSHARE_', 'HITHINK_', 'XIAODEFA_', 'DEEPSEEK_', 'STOCKDATA_'))}
    environment.update(PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
                       PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', UV_OFFLINE='1')

    def run(name, command):
        result = subprocess.run(list(map(str, command)), env=environment, cwd=ROOT,
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=300)
        (output / (name + '.log')).write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(name + ' failed; retain the attempt and do not overwrite')
        return result.stdout

    target.mkdir(exist_ok=False)
    base_members = {}
    with zipfile.ZipFile(base_archive) as archive:
        for member in archive.infolist():
            parts = PurePosixPath(member.filename.replace('\\', '/')).parts
            if not parts or parts[0] != 'python-collector' or '..' in parts:
                raise ValueError('unexpected base archive namespace')
            if member.is_dir() or len(parts) < 2:
                continue
            relative = PurePosixPath(*parts[1:])
            if (relative.is_absolute() or ':' in str(relative) or
                    (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('linked/unsafe original member')
            name = relative.as_posix()
            if (name.startswith(('Lib/site-packages/', 'Scripts/')) or '__pycache__' in relative.parts
                    or relative.suffix in ('.pyc', '.pyo')
                    or name in ('pyvenv.cfg', 'runtime-manifest.json', 'Lib/EXTERNALLY-MANAGED')):
                continue
            data = archive.read(member)
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(data)
            base_members[name] = hashlib.sha256(data).hexdigest()
    if not {'python.exe', 'python312.dll'} <= base_members.keys():
        raise ValueError('retained archive lacks standalone CPython base')
    python = target / 'python.exe'
    prefix = ("import sys;from pathlib import Path;r=Path(sys.argv[1]).resolve();"
              "assert Path(sys.prefix).resolve()==r==Path(sys.base_prefix).resolve();"
              "assert all(Path(p).resolve().is_relative_to(r) for p in sys.path);print('INDEPENDENT_PREFIX_PASS')")
    run('prefix-before', [python, '-I', '-B', '-X', 'utf8', '-c', prefix, target])
    run('ensurepip', [python, '-I', '-B', '-X', 'utf8', '-m', 'ensurepip', '--default-pip'])
    run('offline-install', [python, '-I', '-B', '-X', 'utf8', '-m', 'pip', 'install', '--no-index',
                           '--no-compile', '--only-binary=:all:', '--require-hashes', '--find-links',
                           wheelhouse, '--find-links', jsonpath_wheel.parent, '-r', lock])
    run('pip-check', [python, '-I', '-B', '-m', 'pip', 'check'])
    run('prefix-after', [python, '-I', '-B', '-c', prefix, target])
    identity = json.loads(run('identity', [python, '-I', '-B', '-c',
        "import sys,json,importlib.metadata as m;import pytest,ruff,duckdb,pandas,pypdf,pyarrow;"
        "print(json.dumps({'python':sys.version,'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'packages':sorted((d.metadata['Name'],d.version) for d in m.distributions())}))"]))
    result = {'at': datetime.now(timezone.utc).isoformat(), 'passed': True,
              'directory': str(target), 'python': str(python), 'identity': identity,
              'base_archive_sha256': base_sha, 'base_files': base_members,
              'repository_locks': {n: digest(ROOT / n) for n in locks},
              'derived_lock_sha256': digest(lock), 'jsonpath_original_lock': original_jsonpath,
              'jsonpath_built_wheel_sha256': jsonpath_sha, 'network_requests': 0,
              'installed_site_packages_copied': False, 'production_or_global_python_changed': False,
              'registered_worktrees_added': 0}
    (output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': True, 'directory': str(target), 'packages': len(identity['packages']),
                      'outside_base_dependency': False}))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-archive', type=Path, required=True)
    parser.add_argument('--base-sha256', required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--jsonpath-wheel', type=Path, required=True)
    parser.add_argument('--jsonpath-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.base_archive.resolve(), args.base_sha256, args.wheelhouse.resolve(),
            args.jsonpath_wheel.resolve(), args.jsonpath_sha256, args.output.resolve())
