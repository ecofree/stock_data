from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from tools.v2 import probe_intake_permissions as probe


def test_publisher_boundary_has_read_only_public_original_and_private_intake(tmp_path):
    checks = probe.access_checks(tmp_path, 'a' * 64, 'publisher')
    assert len(checks) == 24
    allowed = [row for row in checks if row[3]]
    assert allowed == [(tmp_path / 'evidence/objects' / ('a' * 64), 0x80000000, False, True)]
    assert all(not granted for path, mask, directory, granted in checks
               if 'valuation-intake' in str(path) or 'acceptance-inputs' in str(path))
    assert (tmp_path / 'workspace', 0x40, True, False) in checks


def test_system_scope_does_not_claim_system_cannot_write(tmp_path):
    checks = probe.access_checks(tmp_path, 'a' * 64, 'system')
    assert len(checks) == 4 and all(granted for _, _, _, granted in checks)
    with pytest.raises(ValueError):
        probe.access_checks(tmp_path, 'not-a-hash', 'publisher')
    with pytest.raises(ValueError):
        probe.access_checks(tmp_path, 'a' * 64, 'administrator-shortcut')


def test_expired_exact_probe_rejected_before_identity_or_file_access(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    spec = {'schema': 'stockdata_intake_identity_probe_v1',
            'scope': 'file_handles_only_no_material_or_database_writes', 'nonce': 'b' * 32,
            'entry_sha256': hashlib.sha256(Path(probe.__file__).read_bytes()).hexdigest(),
            'window_start': (now - timedelta(hours=2)).isoformat(),
            'window_end': (now - timedelta(hours=1, minutes=1)).isoformat()}
    path = tmp_path / 'expired.json'
    path.write_text(json.dumps(spec))
    monkeypatch.setattr(probe.subprocess, 'check_output', lambda *a, **k: pytest.fail('identity accessed'))
    monkeypatch.setattr(probe, 'file_access', lambda *a, **k: pytest.fail('access attempted'))
    with pytest.raises(ValueError, match='fixed actual-identity'):
        probe.main(['--spec', str(path), '--spec-sha256', hashlib.sha256(path.read_bytes()).hexdigest()])
