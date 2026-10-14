"""Nonce-bound real identity file-handle proof; never reads secrets or writes business data."""
import argparse
import csv
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

PUBLISHER_SID = 'S-1-5-21-3027070730-734606845-1610825463-1006'


def access_checks(root, sample_sha, role):
    root = Path(root)
    if not re.fullmatch('[a-f0-9]{64}', sample_sha):
        raise ValueError('exact public original SHA required')
    if role == 'publisher':
        checks = [(root / 'evidence/objects' / sample_sha, 0x80000000, False, True)]
        checks += [(root / 'evidence/objects' / sample_sha, rights, False, False)
                   for rights in (0x40000000, 0x10000, 0x40000)]
        for name in ('evidence', 'evidence/objects', 'workspace/valuation-intake', 'workspace/acceptance-inputs'):
            checks += [(root / name, rights, True, False) for rights in (2, 4, 0x40, 0x10000)]
        checks += [(root / name, 0x80000000, False, False) for name in (
            'config/collector.env', 'workspace/valuation-intake/README.md',
            'workspace/acceptance-inputs/README.md')]
        checks += [(root / 'workspace', 0x40, True, False)]
        return checks
    if role == 'system':
        return [(root / 'evidence/objects' / sample_sha, 0x80000000, False, True),
                (root / 'config/collector.env', 0x80000000, False, True),
                (root / 'workspace/valuation-intake', 1, True, True),
                (root / 'workspace/valuation-intake/README.md', 0x80000000, False, True)]
    raise ValueError('explicit publisher or system role required')


def file_access(path, rights, directory):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), rights, 7, None, 3, 0x02000000 if directory else 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return False, ctypes.get_last_error()
    kernel.CloseHandle(handle)
    return True, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--spec-sha256', required=True)
    args = parser.parse_args(argv)
    raw = args.spec.read_bytes()
    if len(raw) > 100000 or hashlib.sha256(raw).hexdigest() != args.spec_sha256:
        raise ValueError('exact bounded probe specification required')
    spec = json.loads(raw)
    if (spec.get('schema') != 'stockdata_intake_identity_probe_v1'
            or spec.get('scope') != 'file_handles_only_no_material_or_database_writes'
            or not re.fullmatch('[a-f0-9]{32}', spec.get('nonce', ''))
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != spec.get('entry_sha256')):
        raise ValueError('probe specification/entry scope mismatch')
    start, end = (datetime.fromisoformat(spec[n]) for n in ('window_start', 'window_end'))
    now = datetime.now(timezone.utc)
    if (start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 3600
            or not start <= now < end):
        raise ValueError('outside fixed actual-identity probe window')
    expected = PUBLISHER_SID if spec['role'] == 'publisher' else 'S-1-5-18'
    actual = next(csv.reader(subprocess.check_output(
        ['whoami.exe', '/user', '/fo', 'csv', '/nh'], text=True).strip().splitlines()))[-1]
    if actual != expected or spec.get('sid') != expected or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ValueError('actual dedicated identity and isolated no-bytecode interpreter required')
    root, output = Path(spec['runtime_root']), Path(spec['output'])
    if (not root.is_absolute() or output.exists()
            or not output.is_relative_to(root / 'workspace')
            or output.name != 'intake-permission-probe-' + spec['nonce']):
        raise ValueError('exact new owned workspace proof output required')
    checks = access_checks(root, spec['sample_sha256'], spec['role'])
    result = []
    for path, rights, directory, expected_granted in checks:
        if datetime.now(timezone.utc) >= end:
            raise ValueError('probe expired without renewal')
        granted, error = file_access(path, rights, directory)
        if granted != expected_granted or (not granted and error != 5):
            raise ValueError('required file access boundary failed; sharing/missing errors are not ACL proof')
        result.append({'path': str(path), 'rights': rights, 'granted': granted, 'winerror': error})
    output.mkdir(exist_ok=False)
    receipt = {'at': datetime.now(timezone.utc).isoformat(), 'passed': True,
               'nonce': spec['nonce'], 'sid': actual, 'role': spec['role'],
               'checked_access_requests': len(result), 'checks': result,
               'secret_bytes_read': 0, 'materials_or_business_database_writes': 0,
               'account_or_policy_changes': 0, 'financial_qualification_granted': False,
               'system_privilege_not_misrepresented_as_readonly': True}
    (output / 'identity-result.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': True, 'role': spec['role'], 'nonce': spec['nonce'],
                      'checked_access_requests': len(result)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
