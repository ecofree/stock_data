"""Behavioral counterexamples for the current engineering boundary audit."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_offline_acceptance import fake_package
from test_verification_hygiene import delivery_fixture
from tools.v2 import prepare_development_runtime as builder
from tools.v2.offline_contract import LOCKS


def test_builder_supports_python_without_path_is_junction(tmp_path, monkeypatch):
    root, _ = fake_package(tmp_path)
    base = tmp_path / 'builder'
    base.mkdir()
    for name in LOCKS:
        shutil.copyfile(root / 'code' / name, base / name)
    monkeypatch.setattr(builder, 'ROOT', base)
    monkeypatch.delattr(Path, 'is_junction', raising=False)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 300, output='real prefix timeout')

    monkeypatch.setattr(builder.subprocess, 'run', timeout)
    wheel = root / 'environment/wheels/jsonpath-0.82.2-py3-none-any.whl'
    with pytest.raises(subprocess.TimeoutExpired):
        builder.prepare(root / 'environment/base.zip', builder.digest(root / 'environment/base.zip'),
                        wheel.parent, wheel, builder.digest(wheel), tmp_path / 'evidence')
    assert 'real prefix timeout' in (tmp_path / 'evidence/prefix-before.log').read_text()
    assert not (tmp_path / 'evidence/verification.json').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction contract')
@pytest.mark.parametrize('destination', [False, True])
def test_actual_junction_is_rejected_before_input_access_or_output_creation(tmp_path, monkeypatch, destination):
    target = tmp_path / 'unlinked'; target.mkdir()
    link = tmp_path / 'linked'
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                    'New-Item -ItemType Junction -Path ' + quote(link) + ' -Target ' + quote(target) + ' | Out-Null'],
                   check=True, capture_output=True, timeout=20)
    monkeypatch.setattr(builder, 'ROOT', tmp_path / 'source')
    # Without lexical-path checks these missing inputs fail with FileNotFoundError.
    archive = (tmp_path if destination else link) / 'missing-base.zip'
    output = (link if destination else tmp_path) / 'evidence'
    try:
        with pytest.raises(ValueError, match='linked input/output'):
            builder.prepare(archive, '0' * 64, tmp_path / 'wheels', tmp_path / 'wheel.whl', '0' * 64, output)
        assert not (tmp_path / 'source/.venv').exists()
        assert not (target / 'evidence').exists()
    finally:
        # Remove only the exact test-created junction itself, never its target.
        link.rmdir()


@pytest.mark.parametrize('declared_scope', ['market_research_observation_v1', 'historical_account_scope', None])
def test_engineering_receipt_never_guesses_user_account_acceptance(tmp_path, monkeypatch, declared_scope):
    delivery, wheel, _ = delivery_fixture(tmp_path, monkeypatch)
    (delivery.ROOT / 'workspace-config.json').write_text(json.dumps({'acceptance_scope': declared_scope}))
    assert delivery.capture(tmp_path / 'proof', Path(sys.executable), wheel, minimal_runtime=True)
    receipt = json.loads((tmp_path / 'proof/verification.json').read_text('utf-8'))
    assert receipt['real_account_acceptance'] == 'not_assessed_by_code_verification'
    assert receipt['business_acceptance'] == 'not_performed'
    assert receipt['execution_ready'] is False and receipt['production_cutover'] is False


def test_ci_style_isolated_interpreter_rejects_host_package_contamination(tmp_path):
    host = tmp_path / 'host-packages'; host.mkdir()
    metadata = host / 'pipx-999.dist-info'; metadata.mkdir()
    (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: pipx\nVersion: 999\nRequires-Dist: filelock>=999\n')
    env = os.environ.copy()
    env.update(PYTHONPATH=str(host), PYTHONDONTWRITEBYTECODE='1')
    visible = subprocess.run([sys.executable, '-B', '-c',
        "import importlib.metadata as m;assert m.version('pipx')=='999'"], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=20)
    assert visible.returncode == 0, visible.stderr
    runtime = tmp_path / 'isolated'
    subprocess.run([sys.executable, '-I', '-B', '-m', 'venv', '--without-pip', str(runtime)],
                   env=env, check=True, capture_output=True, timeout=30)
    python = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    result = subprocess.run([str(python), '-I', '-B', '-c',
        "import sys,site,importlib.metadata as m;assert sys.prefix!=sys.base_prefix;"
        "assert not site.ENABLE_USER_SITE;assert not list(m.distributions(name='pipx'));print('ISOLATED')"],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout.strip() == 'ISOLATED', result.stderr
