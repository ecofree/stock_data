"""Reuse only a fresh exact full local data gate; never a green flag, old XML or business permit."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.v2.verify_delivery import source_files, validate_wheel_content, data_lane_selectors  # noqa: E402

CHECKS = frozenset(('lint', 'migrations', 'writer_boundary', 'tests', 'installed_dependencies', 'installed_core'))
MAX_AGE_SECONDS = 2 * 60 * 60


def clean_environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ('GIT_', 'KPL_', 'TUSHARE_', 'HITHINK_', 'XIAODEFA_', 'DEEPSEEK_', 'STOCKDATA_RUNTIME_'))}
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def current_identity():
    env = clean_environment()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, env=env, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'],
                                        cwd=ROOT, env=env).strip())
    return head, dirty


def current_data_test_count():
    # Collection only, no fixture/test execution or additional expanded run.
    unused_cache = Path(tempfile.gettempdir()) / ('stockdata-collection-cache-' + uuid4().hex)
    command = [sys.executable, '-I', '-B', '-X', 'utf8', '-X', 'pycache_prefix=' + str(unused_cache),
               '-m', 'pytest', '-o', 'addopts=', '--collect-only', '-q', '--color=no', '-p', 'no:cacheprovider',
               *data_lane_selectors()]
    result = subprocess.run(command, cwd=ROOT, env=clean_environment(),
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    matches = re.findall(r'(?:^|\n)(\d+)(?:/\d+)? tests? collected\b', result.stdout)
    if result.returncode != 0 or len(matches) != 1 or int(matches[0]) < 1:
        raise ValueError('cannot verify current nonempty complete data collection')
    return int(matches[0])


def digest(path):
    path = Path(path)
    if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (path, *path.parents)):
        raise ValueError('linked verification artifacts rejected')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_evidence(path, expected_sha256, *, now=None):
    path = Path(path)
    if (not re.fullmatch('[a-f0-9]{64}', expected_sha256) or path.stat().st_size > 2_000_000
            or digest(path) != expected_sha256):
        raise ValueError('exact bounded verification SHA required')
    report = json.loads(path.read_text('utf-8'))
    clock = now or datetime.now(timezone.utc)
    completed = datetime.fromisoformat(report.get('completed_at', ''))
    if (clock.tzinfo is None or completed.tzinfo is None
            or not 0 <= (clock - completed).total_seconds() <= MAX_AGE_SECONDS):
        raise ValueError('fresh non-future verification required; maintenance windows are not renewed')
    head, dirty = current_identity()
    if (dirty or report.get('base_commit') != head or report.get('worktree_is_dirty') is not False
            or report.get('scope') != 'local_code_and_installed_core_not_business_or_production_acceptance'
            or report.get('local_checks_passed') is not True
            or report.get('source_unchanged_during_checks') is not True
            or report.get('minimal_runtime_required') is not True or report.get('test_lane') != 'data'):
        raise ValueError('clean exact commit and complete data/minimal-core gate required')
    current = source_files(env=clean_environment())
    tested = report.get('source_files', {})
    if (not current or not tested or set(tested) - set(current) - {'source-distribution.json'}
            or any(tested.get(name) != sha for name, sha in current.items())
            or report.get('source_sha256') != hashlib.sha256(json.dumps(tested, sort_keys=True).encode()).hexdigest()):
        raise ValueError('current source differs from exact verified bytes')
    checks = report.get('checks', {})
    artifacts = report.get('artifacts', {})
    wheel = report.get('wheel', {})
    required = {name + '.log' for name in CHECKS} | {'tests.xml', 'started.json', wheel.get('file', '')}
    if (set(checks) != CHECKS or any(c.get('returncode') != 0 for c in checks.values())
            or not required <= set(artifacts) or not wheel.get('file')):
        raise ValueError('all six checks, original logs, wheel and nonempty JUnit required')
    for name, sha in artifacts.items():
        if Path(name).name != name or '/' in name or '\\' in name or digest(path.parent / name) != sha:
            raise ValueError('verification artifact differs or escapes receipt directory')
    if any(checks[name].get('log_sha256') != artifacts[name + '.log'] for name in CHECKS):
        raise ValueError('check/log binding differs')
    suites = ET.parse(path.parent / 'tests.xml').getroot().iter('testsuite')
    totals = {k: 0 for k in ('tests', 'errors', 'failures', 'skipped')}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, 0))
    if (totals != report.get('tests') or totals['tests'] < 1 or totals['failures'] or totals['errors']
            or totals['tests'] != current_data_test_count()):
        raise ValueError('complete current data test collection and successful JUnit required')
    if wheel.get('sha256') != artifacts[wheel['file']]:
        raise ValueError('installed wheel evidence differs')
    validate_wheel_content(path.parent / wheel['file'], current, root=ROOT)
    if current_identity() != (head, False) or source_files(env=clean_environment()) != current:
        raise ValueError('source changed during evidence readback')
    return {'passed': True, 'commit': head, 'tests': totals, 'verification_sha256': expected_sha256,
            'all_artifact_bytes_reverified': True, 'test_bodies_reexecuted': False,
            'historical_research_CI_still_required': True, 'business_or_production_acceptance': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(validate_evidence(args.proof, args.sha256)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
