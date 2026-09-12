import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import check_kpl_connectivity as canary
from scripts.audit_daily_review_artifact import _embedded_json, audit_artifact
from tools.v2 import capture_windows_cutover as windows


def test_canary_distinguishes_skipped_and_semantic_not_transport(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.stats = dict(success=0, semantic_error=0, empty=0, skipped=0)
            self._circuit_open_reason = None

        def get(self, endpoint, params, **kwargs):
            if endpoint == '/daily':
                self.stats['semantic_error'] += 1
                self.stats['empty'] += 1
                return None
            if endpoint == '/index/zhishu-kline':
                self.stats['skipped'] += 1
                self._circuit_open_reason = 'total_budget_exceeded'
                return None
            self.stats['success'] += 1
            return [{'close': 10}]

    monkeypatch.setattr(canary, 'KPLClient', Client)
    result = canary.run_probe('unused', '2026-09-11', '000001')
    assert result['status'] == 'reachable_partial'
    assert [c['reason'] for c in result['checks']] == [
        'semantic_payload_rejected', 'verified_payload', 'verified_payload', 'not_probed_budget_exhausted']
    assert result['checks'][1]['stats_delta']['empty'] == 0
    assert result['observed_at'] <= result['completed_at']


def test_canary_json_stays_beside_staged_report(tmp_path, monkeypatch):
    result = {'status': 'reachable_partial'}
    monkeypatch.setattr(canary, 'run_probe', lambda *a: result)
    monkeypatch.setattr(canary, 'render_report', lambda *a: 'partial')
    out = tmp_path / 'staged' / 'canary.md'
    monkeypatch.setattr(canary.sys, 'argv', ['probe', '--date', '2026-09-11', '--stock-code', '000001', '--out', str(out)])
    assert canary.main() == 2
    assert json.loads(out.with_suffix('.json').read_text()) == result


def test_duplicate_or_malformed_inline_payload_fails_before_database(tmp_path):
    html = '<script type="application/json" id="trail-data">{}</script>'
    with pytest.raises(ValueError, match='duplicate'):
        _embedded_json(html * 2)
    for value in (html * 2, html.replace('{}', '{bad}')):
        path = tmp_path / 'review.html'; path.write_text(value)
        result = audit_artifact(tmp_path / 'not-created.duckdb', path, '2026-09-11')
        assert result['failures'] == ['inline_payload_invalid_or_duplicate']
        assert not (tmp_path / 'not-created.duckdb').exists()


def test_windows_errors_are_terminating_and_module_path_not_inherited(monkeypatch):
    monkeypatch.setenv('PSModulePath', 'wrong-runtime-modules')
    def run(args, **kwargs):
        assert "$ErrorActionPreference='Stop'" in args[-1]
        assert all(k.upper() != 'PSMODULEPATH' for k in kwargs['env'])
        return SimpleNamespace(returncode=1, stdout='')
    monkeypatch.setattr(windows.subprocess, 'run', run)
    with pytest.raises(RuntimeError):
        windows.powershell('Get-Acl')


def test_empty_acl_cannot_be_certified(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, 'powershell', lambda command: '[]' if 'Get-ScheduledTask ' in command else '{"Owner":null,"Access":[]}')
    with pytest.raises(ValueError, match='ACL'):
        windows.capture(tmp_path / 'capture')
    assert not (tmp_path / 'capture' / 'source-database-acl.json').exists()


def test_hotfix_cannot_be_prepared_in_live_checkout(tmp_path):
    from tools.v2.prepare_legacy_hotfix import prepare
    with pytest.raises(ValueError, match='outside live'):
        prepare(tmp_path, tmp_path / 'unsafe')
    assert not (tmp_path / 'unsafe').exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows script boundary')
def test_research_release_hash_mismatch_stops_before_update(tmp_path):
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    (workspace / 'workspace-config.json').write_text('{}')
    release = tmp_path / 'release'; release.mkdir()
    (release / 'research-release.json').write_text('{}')
    environment = tmp_path / 'config.env'; environment.write_text('')
    script = Path(__file__).resolve().parents[1] / 'scripts/run_research_daily.ps1'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script),
        '-Python', sys.executable, '-Workspace', str(workspace), '-EnvironmentFile', str(environment),
        '-ReleaseDirectory', str(release), '-ReleaseManifestSha256', '0' * 64],
        capture_output=True, text=True, env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'}, timeout=30)
    assert result.returncode != 0
    assert 'Release manifest differs' in result.stderr
    assert not (workspace / 'scheduled-logs').exists()
