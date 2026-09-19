"""Run as the new research identity: prove read/write boundaries without orders or network."""
import argparse
import csv
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import re
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
        error = getattr(exc, 'winerror', None)
        if error is None and sys.platform == 'win32':
            # CRT open reports EACCES without the underlying Windows error.
            # Ask Win32 directly so a sharing violation cannot pass as an ACL denial.
            return directory_write_denied(path, flags=0)
        return error == 5


def directory_write_denied(path, *, flags=0x02000000):
    """Request write-data/add-file on an existing object without modifying it."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x0002, 7, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() == 5
    kernel.CloseHandle(handle)
    return False


def product_contract(data, expected_date):
    """Accept current market observations, never promote an older forecast."""
    from trade_system.v2.domain import identity
    market = data.get('market') or {}
    if (not re.fullmatch(r'\d{4}-\d{2}-\d{2}', expected_date)
            or market.get('trade_date') != expected_date or not market.get('stocks')):
        raise ValueError('nonempty expected market session required; prior publication is not current')
    if (market.get('scope') != 'read_only_market_review_not_execution'
            or market.get('execution_ready') is not False or data.get('execution_ready') is not False
            or market.get('snapshot_id') != identity({k:v for k,v in market.items() if k != 'snapshot_id'})):
        raise ValueError('qualified non-executable market snapshot required')
    prediction = data.get('prediction')
    historical = bool(prediction and prediction['date'] != expected_date)
    if prediction:
        if prediction['date'] > expected_date:
            raise ValueError('future prediction cannot accompany an earlier market session')
        if (data.get('prediction_matches_market') is not (not historical)
                or (historical and data.get('research_status') != 'historical_prediction_not_current_candidates')):
            raise ValueError('historical prediction must be explicitly excluded from current candidates')
    return {'market_date':market['trade_date'], 'market_snapshot_id':market['snapshot_id'],
            'market_securities':len(market['stocks']), 'calendar_state':market.get('session_state'),
            'prediction_date':prediction['date'] if prediction else None,
            'predictions':prediction['predictions'] if prediction else 0,
            'historical_prediction':historical, 'prediction_matches_market':data.get('prediction_matches_market'),
            'research_status':data.get('research_status'), 'data_qualification':'reported_not_upgraded'}


def inspect_product(workspace, database, expected_date):
    """Read only. No inference, model refresh, journal rebuild or publication."""
    from trade_system.v2.market_workspace import latest_snapshot
    from trade_system.v2.research_product import saved_projection, read_prediction
    config = json.loads((Path(workspace)/'workspace-config.json').read_text(encoding='utf-8'))
    if config.get('read_only') is not True or Path(config['market_database']).resolve() != Path(database).resolve():
        raise ValueError('workspace must bind the same read-only source database')
    current = latest_snapshot(database, datetime.now(timezone.utc).isoformat())
    if current['trade_date'] != expected_date:
        raise ValueError('source database has no expected market session')
    data = saved_projection(workspace)
    result = product_contract(data, expected_date)
    frozen = read_prediction(workspace)
    if (frozen or {}).get('prediction_id') != (data.get('prediction') or {}).get('prediction_id'):
        raise ValueError('published prediction differs from the verified frozen pointer')
    return data, result


def run(release, workspace, database, output, nonce, expected_sid, env_file,
        *, expected_date, manifest_sha256, runtime):
    release, workspace, output = map(Path, (release, workspace, output))
    row = next(csv.reader(subprocess.check_output(
        ['whoami.exe', '/user', '/fo', 'csv', '/nh'], text=True).strip().splitlines()))
    if row[-1].upper() != expected_sid.upper():
        raise ValueError('probe did not run as the newly registered identity')
    if not re.fullmatch('[a-f0-9]{32}', nonce):
        raise ValueError('fresh hexadecimal probe nonce required')
    runtime = Path(runtime).resolve(strict=True)
    if (not sys.flags.isolated or Path(sys.prefix).resolve() != runtime
            or Path(sys.base_prefix).resolve() != runtime
            or any(not Path(p).resolve().is_relative_to(runtime) for p in sys.path)):
        raise ValueError('isolated standalone runtime required; no global or user package paths')
    manifest_path = release / 'research-release.json'
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_sha256:
        raise ValueError('release manifest differs from the separately approved hash')
    manifest = json.loads(manifest_path.read_text())
    if {p.relative_to(release).as_posix() for p in release.rglob('*.py')} != {
            name for name in manifest['files'] if name.endswith('.py')}:
        raise ValueError('missing or unexpected release Python modules')
    for name, digest in manifest['files'].items():
        p = release / name
        if release.resolve() not in p.resolve().parents or p.is_symlink():
            raise ValueError('unsafe release member')
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError('release changed')
    sys.path.insert(0, str(release.resolve()))
    from trade_system.v2.research_product import read_build
    from trade_system.v2.research_product_view import render, export_projection
    data, contract = inspect_product(workspace, database, expected_date)
    with Path(database).open('rb') as f:
        if not f.read(16): raise ValueError('source database unreadable')
    frozen = [Path(database), workspace / 'workspace-config.json', workspace / 'judgement.guard',
              release / 'run_research.py', Path(env_file), Path(sys.executable)]
    if (workspace / 'research-current.json').exists():
        _, model, _ = read_build(workspace, historical=True)
        frozen += [Path(model['model_path']), workspace / 'research-current.json']
    denied = {str(p): write_denied(p) for p in frozen}
    if not all(denied.values()):
        raise ValueError('write protection not verified: ' + ', '.join(p for p, ok in denied.items() if not ok))
    if not directory_write_denied(workspace / 'notes'):
        raise ValueError('automated research identity can create human judgement records')
    runtime_denied = {str(p):directory_write_denied(p) for p in
                      (runtime, runtime/'Lib', runtime/'Lib/site-packages', release)}
    if not all(runtime_denied.values()):
        raise ValueError('research identity can add runtime or release modules')
    # Only new, nonce-scoped evidence is created. Never rewrite a human note.
    output.mkdir(parents=True, exist_ok=False)
    root_test = workspace / ('deployment-write-test-' + nonce + '.json')
    with root_test.open('x', encoding='utf-8') as f:
        json.dump({'nonce': nonce, 'scope': 'permission_probe_not_human_judgement'}, f)
    exported = export_projection(data)
    (output / 'index.html').write_text(render(exported), encoding='utf-8')
    (output / 'desk.json').write_text(json.dumps(exported, ensure_ascii=False), encoding='utf-8')
    result = {'nonce': nonce, 'sid': expected_sid, 'passed': True,
              'observed_at': datetime.now(timezone.utc).isoformat(),
              'protected_write_denied': denied, 'workspace_write_verified': True,
              'human_note_creation_denied': True,
              'source_database_read_only_query_verified': True,
              **contract, 'runtime_directory_write_denied':runtime_denied,
              'release_manifest_sha256':manifest_sha256, 'runtime':str(runtime),
              'permission_scope':'listed_files_and_directories_not_recursive_ACL_audit',
              'provider_requests': 0, 'fits': 0, 'execution_ready': False, 'production_cutover':False}
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('release', 'workspace', 'database', 'output', 'nonce', 'sid', 'environment',
                 'expected-date', 'manifest-sha256', 'runtime'):
        p.add_argument('--' + name, required=True)
    a = p.parse_args()
    print(json.dumps(run(a.release, a.workspace, a.database, a.output, a.nonce, a.sid, a.environment,
                         expected_date=a.expected_date, manifest_sha256=a.manifest_sha256, runtime=a.runtime)))
