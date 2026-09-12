"""Run as the new research identity: prove read/write boundaries without orders or network."""
import argparse
import csv
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


def write_denied(path):
    # Opening an existing file does not truncate or write it. An unexpected
    # granted handle is closed and reported as a failed permission boundary.
    try:
        with Path(path).open('r+b'):
            return False
    except PermissionError as exc:
        return getattr(exc, 'winerror', None) == 5


def directory_write_denied(path):
    """Request an existing directory's add-file right; never create a note."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x0002, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() == 5
    kernel.CloseHandle(handle)
    return False


def run(release, workspace, database, output, nonce, expected_sid, env_file):
    release, workspace, output = map(Path, (release, workspace, output))
    row = next(csv.reader(subprocess.check_output(
        ['whoami.exe', '/user', '/fo', 'csv', '/nh'], text=True).strip().splitlines()))
    if row[-1].upper() != expected_sid.upper():
        raise ValueError('probe did not run as the newly registered identity')
    manifest = json.loads((release / 'research-release.json').read_text())
    for name, digest in manifest['files'].items():
        p = release / name
        if release.resolve() not in p.resolve().parents or p.is_symlink():
            raise ValueError('unsafe release member')
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError('release changed')
    sys.path.insert(0, str(release.resolve()))
    from trade_system.v2.research_product import read_build, read_prediction, render_saved
    _, model, _ = read_build(workspace)
    prediction = read_prediction(workspace)
    import duckdb
    with duckdb.connect(str(database), read_only=True) as connection:
        latest = connection.execute('SELECT max(CAST(trade_date AS DATE)) FROM v_default_concept_daily').fetchone()[0]
    if str(latest) != prediction['date']:
        raise ValueError('source concept snapshot and frozen prediction date differ')
    with Path(database).open('rb') as f:
        if not f.read(16): raise ValueError('source database unreadable')
    frozen = [Path(database), Path(model['model_path']), workspace / 'research-current.json',
              workspace / 'workspace-config.json', workspace / 'judgement.guard',
              release / 'run_research.py', Path(env_file)]
    denied = {str(p): write_denied(p) for p in frozen}
    if not all(denied.values()):
        raise ValueError('research identity can modify protected source/model/configuration')
    if not directory_write_denied(workspace / 'notes'):
        raise ValueError('automated research identity can create human judgement records')
    # Only new, nonce-scoped evidence is created. Never rewrite a human note.
    output.mkdir(parents=True, exist_ok=False)
    root_test = workspace / ('deployment-write-test-' + nonce + '.json')
    with root_test.open('x', encoding='utf-8') as f:
        json.dump({'nonce': nonce, 'scope': 'permission_probe_not_human_judgement'}, f)
    render_saved(workspace, output / 'render')
    result = {'nonce': nonce, 'sid': expected_sid, 'passed': True,
              'observed_at': datetime.now(timezone.utc).isoformat(),
              'protected_write_denied': denied, 'workspace_write_verified': True,
              'human_note_creation_denied': True,
              'source_database_read_only_query_verified': True,
              'predictions': prediction['predictions'], 'prediction_date': prediction['date'],
              'provider_requests': 0, 'fits': 0, 'execution_ready': False}
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('release', 'workspace', 'database', 'output', 'nonce', 'sid', 'environment'):
        p.add_argument('--' + name, required=True)
    a = p.parse_args()
    print(json.dumps(run(a.release, a.workspace, a.database, a.output, a.nonce, a.sid, a.environment)))
