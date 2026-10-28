"""A sealed source's real bytes, not a manifest claim, establish its identity."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_system import pipeline_runtime as runtime


def sealed(tmp_path, monkeypatch):
    root = tmp_path / 'source'; root.mkdir()
    contents = {
        'scripts/run_integrated_daily.py': 'import trade_system.consumer\n',
        'trade_system/config.py': '# configuration\n',
        'trade_system/schema.py': '# schema\n',
        'trade_system/consumer.py': '# consumer\n',
        'pyproject.toml': '# project\n',
        'config/thresholds.json': '{"limit":7}\n',
    }
    for name, text in contents.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    manifest = {'schema': 'source_distribution_v1',
                'scope': 'source_identity_only_not_deployment_authority',
                'source_commit': 'a' * 40,
                'files': {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in contents}}
    marker = root / 'source-distribution.json'
    marker.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(runtime, 'PROJECT_ROOT', root)
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=128, stdout='', stderr='no Git'))
    return root, marker, manifest


def test_verified_git_free_source_has_explicit_identity_without_faking_git_or_permission(tmp_path, monkeypatch):
    root, marker, manifest = sealed(tmp_path, monkeypatch)
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = runtime.runtime_fingerprint()
    assert result['fingerprint_complete'] and result['fingerprint_errors'] == []
    assert result['identity_kind'] == 'source_distribution'
    assert result['source_commit'] == manifest['source_commit']
    assert result['source_distribution_sha256'] == hashlib.sha256(marker.read_bytes()).hexdigest()
    assert result['source_distribution_file_count'] == len(manifest['files'])
    assert result['git_commit'] == 'unknown' and result['worktree_dirty'] is None
    assert 'not_deployment_authority' in result['identity_scope']
    assert 'execution_ready' not in result and 'production_cutover' not in result
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('damage', ['missing_member', 'altered_member', 'missing_marker', 'bad_json',
                                  'bad_commit', 'bad_scope', 'escape', 'case_alias', 'duplicate_key',
                                  'unlisted_consumer', 'unlisted_config'])
def test_sealed_identity_refuses_damage_omission_and_unbound_active_inputs(tmp_path, monkeypatch, damage):
    root, marker, manifest = sealed(tmp_path, monkeypatch)
    if damage == 'missing_member': (root/'trade_system/consumer.py').unlink()
    elif damage == 'altered_member': (root/'trade_system/consumer.py').write_text('# changed')
    elif damage == 'missing_marker': marker.unlink()
    elif damage == 'bad_json': marker.write_text('{')
    elif damage == 'duplicate_key':
        marker.write_text(marker.read_text().replace('"source_commit":', '"source_commit":"b", "source_commit":'))
    else:
        if damage == 'bad_commit': manifest['source_commit'] = 'unknown'
        elif damage == 'bad_scope': manifest['scope'] = 'deployment_authority'
        elif damage == 'escape': manifest['files']['../outside.py'] = '0' * 64
        elif damage == 'case_alias': manifest['files']['Trade_System/consumer.py'] = manifest['files']['trade_system/consumer.py']
        elif damage == 'unlisted_consumer': del manifest['files']['trade_system/consumer.py']
        elif damage == 'unlisted_config': del manifest['files']['config/thresholds.json']
        marker.write_text(json.dumps(manifest), encoding='utf-8')
    result = runtime.runtime_fingerprint()
    assert not result['fingerprint_complete'] and result['fingerprint_errors']
    assert result['git_commit'] == 'unknown' and result['worktree_dirty'] is None


def test_source_member_link_is_rejected_on_python311_without_path_is_junction(tmp_path, monkeypatch):
    root, _, _ = sealed(tmp_path, monkeypatch)
    member = root / 'trade_system/consumer.py'
    original = Path.lstat
    monkeypatch.delattr(Path, 'is_junction', raising=False)

    def reparse(path):
        value = original(path)
        if path == member:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, 'lstat', reparse)
    monkeypatch.setattr(runtime.stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400, raising=False)
    result = runtime.runtime_fingerprint()
    assert not result['fingerprint_complete']
    assert 'source_distribution_unverified' in result['fingerprint_errors']
