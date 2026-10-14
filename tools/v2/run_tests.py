"""Run a named test workload into one new owned directory; never a deployment certificate."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.v2.test_profiles import PROFILES  # noqa: E402
from tools.v2.verify_delivery import source_files  # noqa: E402
from trade_system.artifact_retention import reserve_test_run, finish_test_run, owned_test_root  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=PROFILES, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('selectors', nargs='*', help='Optional precise tests/ selectors; never a full-gate claim.')
    args = parser.parse_args(argv)
    if any(not value.startswith('tests/') or '..' in Path(value.split('::')[0]).parts for value in args.selectors):
        parser.error('only explicit tests/ selectors allowed')
    output = args.output.resolve()
    if output.exists():
        parser.error('new evidence directory required')
    for path in (output, *output.parents):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            parser.error('linked evidence directory rejected')
    output.mkdir(parents=True, exist_ok=False)
    before = source_files()
    temporary, owner = reserve_test_run(owned_test_root(), args.profile,
        hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest())
    (output / 'owner.json').write_text(json.dumps(owner, indent=2) + '\n', encoding='utf-8')
    command = [sys.executable, '-I', '-B', '-X', 'utf8', '-X',
               'pycache_prefix=' + str(temporary / 'bytecode-cache'), '-m', 'pytest', '-o', 'addopts=',
               '-q', '-p', 'no:cacheprovider', '--stockdata-profile=' + args.profile,
               '--basetemp=' + str(temporary / 'pytest-temp'), '--junitxml=' + str(output / 'tests.xml'),
               *args.selectors]
    # No forced termination: tests use their own bounded fixtures/child budgets.
    environment = {k: v for k, v in os.environ.items() if not k.startswith(
        ('KPL_', 'TUSHARE_', 'HITHINK_', 'XIAODEFA_', 'DEEPSEEK_', 'STOCKDATA_RUNTIME_'))}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['PYTHONPYCACHEPREFIX'] = str(temporary / 'bytecode-cache')
    result = subprocess.run(command, cwd=ROOT, env=environment,
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'tests.log').write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
    totals = {k: 0 for k in ('tests', 'errors', 'failures', 'skipped')}
    if (output / 'tests.xml').is_file():
        for suite in ET.parse(output / 'tests.xml').getroot().iter('testsuite'):
            for key in totals:
                totals[key] += int(suite.attrib.get(key, 0))
    stable = before == source_files()
    passed = result.returncode == 0 and stable and totals['tests'] > 0 and not (totals['errors'] or totals['failures'])
    receipt = {**owner, 'finished_at': datetime.now(timezone.utc).isoformat(), 'passed': passed,
               'native_exit_code': result.returncode, 'totals': totals, 'source_unchanged': stable,
               'release_gate_complete': False, 'business_acceptance': False,
               'selectors': args.selectors, 'temporary_artifacts_owner': str(temporary / 'owner.json'),
               'cleanup_requires_policy_and_restore_or_disposable_fixture_proof': True}
    (output / 'verification.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    finish_test_run(temporary, owner, {'passed': passed, 'evidence': str(output / 'verification.json')})
    print(json.dumps({'passed': passed, 'profile': args.profile, 'totals': totals,
                      'release_gate_complete': False, 'output': str(output)}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
