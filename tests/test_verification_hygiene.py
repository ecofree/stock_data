"""Verification subprocesses must not mutate sealed runtimes or report empty test proof."""
from types import SimpleNamespace
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest


def delivery_fixture(tmp_path, monkeypatch, *, test_xml=True):
    from tools.v2 import verify_delivery as delivery
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'pyproject.toml').write_text(
        '[tool.stock_data.release]\nmodules=["trade_system.__init__"]\n', encoding='utf-8')
    content = b'# synthetic wheel member\n'
    fingerprint = {'trade_system/__init__.py': hashlib.sha256(content).hexdigest()}
    wheel = tmp_path / 'synthetic.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('trade_system/__init__.py', content)
    monkeypatch.setattr(delivery, 'ROOT', root)
    monkeypatch.setattr(delivery, 'owned_test_root', lambda: tmp_path / 'owned-runs')
    monkeypatch.setattr(delivery, 'source_files', lambda: fingerprint.copy())
    monkeypatch.setattr(delivery.subprocess, 'check_output',
                        lambda command, **kw: '' if 'status' in command else 'fixture')
    commands = {}

    def run(output, name, command, **kwargs):
        commands[name] = command
        if name == 'tests' and test_xml:
            (output / 'tests.xml').write_text(
                '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>',
                encoding='utf-8')
        return {'returncode': 0, 'log_sha256': '0' * 64}

    monkeypatch.setattr(delivery, '_run_check', run)
    return delivery, wheel, commands


def test_delivery_all_python_children_disable_bytecode(tmp_path, monkeypatch):
    delivery, wheel, commands = delivery_fixture(tmp_path, monkeypatch)
    assert delivery.capture(tmp_path / 'proof', Path(sys.executable), wheel, minimal_runtime=True)
    assert len(commands) == 6
    for name, command in commands.items():
        assert '-B' in command, name
        assert command.index('-B') < min(
            (i for i, value in enumerate(command) if value == '-m' or str(value).endswith('.py')),
            default=len(command)), name
    assert '-I' in commands['installed_core']


def test_delivery_zero_exit_without_junit_is_not_a_pass(tmp_path, monkeypatch):
    delivery, wheel, _ = delivery_fixture(tmp_path, monkeypatch, test_xml=False)
    assert not delivery.capture(tmp_path / 'proof', Path(sys.executable), wheel)
    proof = json.loads((tmp_path / 'proof/verification.json').read_text('utf-8'))
    assert proof['local_checks_passed'] is False


@pytest.mark.parametrize('minimal', [False, True])
def test_installed_probe_nested_children_keep_bytecode_disabled(tmp_path, monkeypatch, minimal):
    from tools.v2 import probe_installed as probe
    calls = []
    monkeypatch.setattr(probe.trade_system, '__file__',
                        str(tmp_path / 'site-packages/trade_system/__init__.py'))
    monkeypatch.setattr(probe, 'find_spec', lambda name: None)
    monkeypatch.setattr(probe, 'paper_restart_probe', lambda folder: None)
    monkeypatch.setattr(sys, 'argv', ['probe_installed.py'] + (['--minimal-runtime'] if minimal else []))

    def run(command, **kwargs):
        calls.append(command)
        stdout = ('confirm capture judge' if '--help' in command else
                  json.dumps({'ok': True, 'result': {'execution_ready': False}}))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    monkeypatch.setattr(probe.subprocess, 'run', run)
    probe.main()
    assert len(calls) == 3
    assert all('-I' in command and '-B' in command for command in calls)


def test_schema_diagnostics_do_not_pollute_machine_stdout(tmp_path):
    import os
    import subprocess
    root = Path(__file__).resolve().parents[1]
    script = ("import sys,json;sys.path.insert(0,sys.argv[1]);"
              "from trade_system.data_store import DuckDBStore;from trade_system.schema import init_schema;"
              "s=DuckDBStore(sys.argv[2]);init_schema(s.conn);s.close();print(json.dumps({'ok':True}))")
    env = {k: v for k, v in os.environ.items() if not k.startswith(('KPL_', 'STOCKDATA_'))}
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', script,
                             str(root), str(tmp_path / 'machine-protocol.duckdb')],
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'ok': True}
    assert 'Schema initialized successfully' in result.stderr


@pytest.mark.parametrize('relative', [
    'trade_system/migration_boundary.py', 'trade_system/v2/daily_case_run.py',
    'trade_system/v2/research_program.py', 'tools/v2/rehearse_research_release.py',
])
def test_other_isolated_runtime_children_cannot_ignore_bytecode_policy(relative):
    import ast
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / relative).read_text('utf-8-sig'))
    commands = [[item.value for item in node.elts if isinstance(item, ast.Constant)]
                for node in ast.walk(tree) if isinstance(node, ast.List)]
    isolated = [values for values in commands if '-I' in values]
    assert isolated, 'expected runtime subprocess coverage'
    assert all('-B' in values for values in isolated), relative
