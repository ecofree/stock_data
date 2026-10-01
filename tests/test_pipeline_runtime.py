import json
from datetime import datetime, timedelta
import os
import hashlib

import pytest

from trade_system.pipeline_runtime import (
    PipelineAlreadyRunning,
    PipelineLock,
    RunManifest,
    all_manifests,
    default_observation_policy,
    latest_manifests,
    load_observation_contract,
    observation_windows,
)


def test_pipeline_lock_blocks_concurrent_run_and_releases(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    db_path.touch()

    with PipelineLock(db_path, "run-one"):
        with pytest.raises(PipelineAlreadyRunning):
            with PipelineLock(db_path, "run-two"):
                pass
    with PipelineLock(db_path, "run-three"):
        assert db_path.with_name("sample.duckdb.pipeline.lock").exists()


def test_runtime_fingerprint_tracks_consumers_and_actual_thresholds_not_git_failure_as_clean(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from trade_system import pipeline_runtime as runtime

    for name in ("scripts/run_integrated_daily.py", "trade_system/config.py", "trade_system/schema.py", "trade_system/consumer.py",
                 "config/phase_thresholds.json", "pyproject.toml"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if path.suffix == ".json" else "# fixture", encoding="utf-8")
    (tmp_path / "scripts/run_integrated_daily.py").write_text("import trade_system.consumer", encoding="utf-8")
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=128, stdout="", stderr="synthetic Git failure"))
    first = runtime.runtime_fingerprint()
    assert first["worktree_dirty"] is None and first["git_commit"] == "unknown"
    assert not first["fingerprint_complete"]
    (tmp_path / "trade_system/consumer.py").write_text("# changed consumer", encoding="utf-8")
    (tmp_path / "config/phase_thresholds.json").write_text('{"limit": 7}', encoding="utf-8")
    (tmp_path / 'research').mkdir()
    (tmp_path / 'research/unrelated.py').write_text('# ignored research', encoding='utf-8')
    second = runtime.runtime_fingerprint()
    assert first['source_file_count'] == second['source_file_count'] == 2
    assert first["source_hash"] != second["source_hash"]
    assert first["config_hash"] != second["config_hash"]
    monkeypatch.setattr(runtime, "_file_fingerprint", lambda paths: (_ for _ in ()).throw(OSError("synthetic")))
    assert runtime.runtime_fingerprint()["source_hash"] == "unknown"



def test_run_manifest_is_written_atomically(tmp_path):
    manifest = RunManifest(tmp_path, "run-one", "2026-07-09", "intraday")
    manifest.add_step("step", "completed", ["python", "step.py"], return_code=0)
    manifest.finish("completed")

    data = json.loads(manifest.path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["phase"] == "intraday"
    assert data["steps"][0]["return_code"] == 0
    assert not manifest.path.with_suffix(".json.tmp").exists()


def test_pipeline_lock_does_not_steal_unknown_legacy_lock(tmp_path, monkeypatch):
    db_path = tmp_path / "stale.duckdb"
    db_path.touch()
    lock_path = db_path.with_name("stale.duckdb.pipeline.lock")
    lock_path.write_text(
        json.dumps({
            "run_id": "abandoned",
            "pid": 999999,
            "started_at": (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds"),
            "host": "DESKTOP-TEST",
        }),
        encoding="utf-8",
    )

    def broken_kill(*_args):
        raise SystemError("Windows PID probe failed")

    monkeypatch.setattr(os, "kill", broken_kill)
    with pytest.raises(PipelineAlreadyRunning, match='maintenance'):
        with PipelineLock(db_path, "recovered"):
            pass
    assert json.loads(lock_path.read_text(encoding='utf-8'))['run_id'] == 'abandoned'


def test_pipeline_lock_recovers_abandoned_handle_protocol_without_pid_probe(tmp_path, monkeypatch):
    db_path = tmp_path / "rebooted.duckdb"
    db_path.touch()
    lock_path = db_path.with_name("rebooted.duckdb.pipeline.lock")
    lock_path.write_text(
        json.dumps({
            "run_id": "abandoned-after-reboot",
            "lock_protocol": "os_handle_v2",
            "pid": 999999,
            "started_at": (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds"),
            "host": os.environ.get("COMPUTERNAME") or "unknown",
        }),
        encoding="utf-8",
    )

    def dead_kill(*_args):
        raise ProcessLookupError("dead scheduler child")

    monkeypatch.setattr(os, "kill", dead_kill)
    with PipelineLock(db_path, "recovered-after-reboot"):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["run_id"] == "recovered-after-reboot"


def test_pipeline_lock_does_not_delete_malformed_legacy_lock(tmp_path):
    db_path = tmp_path / "malformed.duckdb"
    db_path.touch()
    lock_path = db_path.with_name("malformed.duckdb.pipeline.lock")
    lock_path.write_text("{truncated", encoding="utf-8")
    old = (datetime.now() - timedelta(minutes=5)).timestamp()
    os.utime(lock_path, (old, old))

    with pytest.raises(PipelineAlreadyRunning, match='maintenance'):
        with PipelineLock(db_path, "recovered-malformed"):
            pass
    assert lock_path.read_text(encoding='utf-8') == '{truncated'


def test_observation_contract_expands_existing_cadence_and_never_invents_legacy_proof(tmp_path, monkeypatch):
    policy = default_observation_policy()
    assert [len(observation_windows(policy, '2026-09-29', phase))
            for phase in ('auction', 'intraday', 'close')] == [5, 49, 1]
    intraday = observation_windows(policy, '2026-09-29', 'intraday')
    assert intraday[23]['window_id'] == 'intraday:11:25:00'
    assert intraday[24]['window_id'] == 'intraday:13:00:00'
    path = tmp_path / 'collector.json'
    raw = json.dumps({'observation_windows': policy}).encode()
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    assert load_observation_contract(path, sha) == policy
    monkeypatch.setattr('trade_system.pipeline_runtime.runtime_fingerprint', lambda: {'fixture': True})
    manifest = RunManifest(tmp_path, 'bound', '2026-09-29', 'intraday')
    manifest.bind_observation_contract(path, sha)
    assert json.loads(manifest.path.read_text())['observation_contract_sha256'] == sha
    path.write_bytes(b'{"version":"old"}')
    with pytest.raises(ValueError, match='SHA256 mismatch'):
        load_observation_contract(path, sha)
    with pytest.raises(ValueError, match='missing or unsupported'):
        load_observation_contract(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize('field,value', [('first_start', '08:50:00'), ('completion_budget_seconds', 91),
                                      ('interval_seconds', 30), ('start_tolerance_seconds', True)])
def test_observation_contract_rejects_preparation_or_renewed_budgets(field, value):
    policy = default_observation_policy()
    policy['phases']['auction'][0][field] = value
    with pytest.raises(ValueError):
        observation_windows(policy, '2026-09-29', 'auction')


def test_all_receipts_preserve_failures_when_latest_is_green(tmp_path):
    for run_id, status, at in (('failed', 'failed', '09:30:00'), ('late-green', 'completed', '10:00:00')):
        folder = tmp_path / 'runs' / run_id
        folder.mkdir(parents=True)
        (folder / 'run.json').write_text(json.dumps({'run_id': run_id, 'trade_date': '2026-09-29',
            'phase': 'intraday', 'started_at': '2026-09-29T' + at, 'status': status}))
    assert {item['status'] for item in all_manifests(tmp_path)} == {'failed', 'completed'}
    assert latest_manifests(tmp_path)[('2026-09-29', 'intraday')]['run_id'] == 'late-green'
