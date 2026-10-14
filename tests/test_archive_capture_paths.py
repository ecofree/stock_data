import hashlib
import json

import pytest

from tools.v2 import capture_windows_cutover as capture


def test_no_capture_mode_implicitly_reads_retired_database_or_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, 'powershell', lambda *a: pytest.fail('implicit retired-root access'))
    with pytest.raises(ValueError, match='explicit current source database'):
        capture.capture(tmp_path / 'snapshot')
    with pytest.raises(ValueError, match='explicit isolated restored root'):
        capture.capture_probe(tmp_path / 'probe.json')


def test_retained_probe_requires_exact_journal_and_explicit_restored_path(tmp_path, monkeypatch):
    restored = tmp_path / 'new isolated restore'
    attempt = restored / 'recovery-probe-0312-once'
    attempt.mkdir(parents=True)
    journal = attempt / 'journal.json'
    journal.write_text('{"status":"historic-failure"}')
    digest = hashlib.sha256(journal.read_bytes()).hexdigest()
    commands = []
    monkeypatch.setattr(capture, 'powershell', lambda command: commands.append(command) or '{}')
    with pytest.raises(ValueError, match='journal SHA'):
        capture.capture_probe(tmp_path / 'bad.json', restored, '0' * 64)
    assert not commands
    capture.capture_probe(tmp_path / 'probe.json', restored, digest)
    assert len(commands) == 1 and str(restored) in commands[0]
    assert 'D:\\accio\\stock-data-runtime' not in commands[0]
    receipt = json.loads((tmp_path / 'probe.json').read_text('utf-8'))
    assert receipt['historical_journal_sha256'] == digest
    assert receipt['current_machine_snapshot_is_not_original_historical_state'] is True
