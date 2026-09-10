from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from trade_system.v2.service import Service
from trade_system.v2.publisher import publish, read_current


def test_multiple_workers_share_one_service_connection(tmp_path):
    with Service(tmp_path / 'live.duckdb') as service:
        service.submit('product', dataset='quotes', unit='CNY', semantics='point', consumer='risk', origin='fixture').result(5)
        with ThreadPoolExecutor(4) as pool:
            requests = list(pool.map(lambda _: service.submit('status'), range(20)))
        assert all(f.result(5)['execution_ready'] is False for f in requests)
        with pytest.raises(ValueError, match='unsupported'):
            service.submit('arbitrary_sql', sql='DROP TABLE fact').result(5)
    with pytest.raises(RuntimeError, match='closed'):
        service.submit('status')
    with Service(tmp_path / 'live.duckdb') as restarted:
        assert restarted.submit('status').result(5)['fact_count'] == 0


def test_bundle_publishes_explicit_dated_csv_and_rejects_old_run(tmp_path):
    publish(tmp_path, 'new', {'review.html': b'new', '2026-09-10.csv': b'code\n000001'}, generation=2)
    with pytest.raises(ValueError, match='older'):
        publish(tmp_path, 'old', {'review.html': b'old'}, generation=1)
    manifest, artifacts = read_current(tmp_path)
    assert manifest['run_id'] == 'new'
    assert artifacts['2026-09-10.csv'].endswith(b'000001')
    (tmp_path / 'runs' / 'new' / 'review.html').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='checksum'):
        read_current(tmp_path)


def test_publish_failure_preserves_previous_complete_pointer(tmp_path, monkeypatch):
    publish(tmp_path, 'first', {'review.html': b'complete'}, generation=1)
    original = Path.replace
    def fail_pointer(self, target):
        if Path(target).name == 'current.json':
            raise OSError('injected disk failure')
        return original(self, target)
    monkeypatch.setattr(Path, 'replace', fail_pointer)
    with pytest.raises(OSError, match='disk failure'):
        publish(tmp_path, 'second', {'review.html': b'new'}, generation=2)
    assert read_current(tmp_path)[1]['review.html'] == b'complete'
    assert json.loads((tmp_path / 'current.json').read_text())['run_id'] == 'first'


def test_bundle_path_escape_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='unsafe'):
        publish(tmp_path / 'reports', 'unsafe', {'../../outside': b'bad'}, generation=1)
    assert not (tmp_path / 'outside').exists()


def test_writer_fatal_failure_resolves_all_pending_requests(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def crash(*_):
        entered.set()
        assert release.wait(5)
        raise SystemExit('injected fatal writer failure')
    monkeypatch.setattr(Service, '_dispatch', staticmethod(crash))
    service = Service(tmp_path / 'fatal.duckdb')
    active = service.submit('status')
    assert entered.wait(5)
    queued = service.submit('status')
    release.set()
    for future in (active, queued):
        with pytest.raises(RuntimeError, match='writer stopped'):
            future.result(5)
    with pytest.raises(RuntimeError, match='closed'):
        service.submit('status')
    service.thread.join(5)
    service.close()
    assert not service.thread.is_alive()


def test_public_jsonl_entrypoint_is_paper_only(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'trade_system.v2', '--db',
                             str(tmp_path / 'cli.duckdb')],
                            input='{"command":"status"}\n', text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    message = json.loads(result.stdout.strip())
    assert message['result']['execution_ready'] is False
    assert message['result']['broker_routing'] == 'disabled'
