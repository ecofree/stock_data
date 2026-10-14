from datetime import datetime, timedelta, timezone
import hashlib
import json
import zipfile

import pytest


def fixture(tmp_path, monkeypatch):
    from tools.v2 import verify_pre_push_evidence as gate
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'pyproject.toml').write_text('[tool.stock_data.release]\nmodules=["module"]\n')
    data = b'# exact synthetic source\n'
    (root / 'module.py').write_bytes(data)
    fingerprint = {'module.py': hashlib.sha256(data).hexdigest()}
    head = 'a' * 40
    now = datetime(2026, 10, 14, 14, tzinfo=timezone.utc)
    folder = tmp_path / 'proof'
    folder.mkdir()
    checks = {}
    for name in ('lint', 'migrations', 'writer_boundary', 'tests', 'installed_dependencies', 'installed_core'):
        content = (name + ': successful synthetic fixture\n').encode()
        (folder / (name + '.log')).write_bytes(content)
        checks[name] = {'returncode': 0, 'log_sha256': hashlib.sha256(content).hexdigest()}
    (folder / 'started.json').write_text('{}')
    (folder / 'tests.xml').write_text('<testsuites><testsuite tests="2" errors="0" failures="0" skipped="0"><testcase name="a"/><testcase name="b"/></testsuite></testsuites>')
    wheel = folder / 'synthetic.whl'
    with zipfile.ZipFile(wheel, 'w') as z:
        z.writestr('module.py', data)
    totals = {'tests': 2, 'errors': 0, 'failures': 0, 'skipped': 0}
    report = {'scope': 'local_code_and_installed_core_not_business_or_production_acceptance',
        'base_commit': head, 'worktree_is_dirty': False, 'source_files': fingerprint.copy(),
        'source_sha256': hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest(),
        'source_unchanged_during_checks': True, 'local_checks_passed': True,
        'minimal_runtime_required': True, 'test_lane': 'data', 'tests': totals,
        'completed_at': (now - timedelta(minutes=10)).isoformat(), 'checks': checks,
        'wheel': {'file': wheel.name, 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}}
    state = {'head': head, 'dirty': False, 'fingerprint': fingerprint, 'count': 2}
    monkeypatch.setattr(gate, 'ROOT', root)
    monkeypatch.setattr(gate, 'current_identity', lambda: (state['head'], state['dirty']))
    monkeypatch.setattr(gate, 'source_files', lambda **kw: state['fingerprint'].copy())
    monkeypatch.setattr(gate, 'current_data_test_count', lambda: state['count'])
    return gate, folder, report, state, now


def write_report(folder, report):
    report['artifacts'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in folder.iterdir() if p.is_file() and p.name != 'verification.json'}
    path = folder / 'verification.json'
    path.write_text(json.dumps(report))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_fresh_exact_full_proof_can_be_reused_without_reexecuting_tests(tmp_path, monkeypatch):
    gate, folder, report, state, now = fixture(tmp_path, monkeypatch)
    path, sha = write_report(folder, report)
    result = gate.validate_evidence(path, sha, now=now)
    assert result['passed'] and result['commit'] == state['head']
    assert result['historical_research_CI_still_required'] is True
    assert result['business_or_production_acceptance'] is False


@pytest.mark.parametrize('change', ['stale', 'future', 'head', 'dirty', 'source',
                                    'missing_log', 'bad_junit', 'empty', 'different_count', 'wheel'])
def test_stale_partial_or_different_proof_never_bypasses_gate(tmp_path, monkeypatch, change):
    gate, folder, report, state, now = fixture(tmp_path, monkeypatch)
    if change == 'stale':
        report['completed_at'] = (now - timedelta(hours=3)).isoformat()
    elif change == 'future':
        report['completed_at'] = (now + timedelta(minutes=1)).isoformat()
    elif change == 'head':
        state['head'] = 'b' * 40
    elif change == 'dirty':
        state['dirty'] = True
    elif change == 'source':
        state['fingerprint']['module.py'] = '0' * 64
    elif change == 'missing_log':
        (folder / 'installed_core.log').unlink()
    elif change == 'bad_junit':
        (folder / 'tests.xml').write_text('<testsuites><testsuite tests="2" errors="0" failures="1" skipped="0"/></testsuites>')
    elif change == 'empty':
        report['tests'] = {'tests': 0, 'errors': 0, 'failures': 0, 'skipped': 0}
        (folder / 'tests.xml').write_text('<testsuites><testsuite tests="0" errors="0" failures="0" skipped="0"/></testsuites>')
        state['count'] = 0
    elif change == 'different_count':
        state['count'] = 3
    elif change == 'wheel':
        with zipfile.ZipFile(folder / 'synthetic.whl', 'w') as z:
            z.writestr('module.py', b'different source')
        report['wheel']['sha256'] = hashlib.sha256((folder / 'synthetic.whl').read_bytes()).hexdigest()
    path, sha = write_report(folder, report)
    with pytest.raises(ValueError):
        gate.validate_evidence(path, sha, now=now)


def test_exact_report_sha_is_required_before_looking_at_green_flags(tmp_path, monkeypatch):
    gate, folder, report, _, now = fixture(tmp_path, monkeypatch)
    path, _ = write_report(folder, report)
    with pytest.raises(ValueError, match='SHA'):
        gate.validate_evidence(path, '0' * 64, now=now)
