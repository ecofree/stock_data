import json
from datetime import datetime, timedelta
import os

import pytest

from trade_system.pipeline_runtime import (
    LatestReportTransaction,
    PipelineAlreadyRunning,
    PipelineLock,
    RunManifest,
    prune_run_reports,
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


def test_run_manifest_is_written_atomically(tmp_path):
    manifest = RunManifest(tmp_path, "run-one", "2026-07-09", "intraday")
    manifest.add_step("step", "completed", ["python", "step.py"], return_code=0)
    manifest.finish("completed")

    data = json.loads(manifest.path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["phase"] == "intraday"
    assert data["steps"][0]["return_code"] == 0
    assert not manifest.path.with_suffix(".json.tmp").exists()


def test_pipeline_lock_recovers_stale_lock_when_windows_pid_probe_errors(tmp_path, monkeypatch):
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
    with PipelineLock(db_path, "recovered"):
        assert lock_path.exists()


def test_failed_run_retains_changed_reports_before_restoring_latest(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    stable = reports / "stable_latest.md"
    changed = reports / "changed_latest.md"
    stable.write_text("stable", encoding="utf-8")
    changed.write_text("previous", encoding="utf-8")

    tx = LatestReportTransaction(reports, "failed-run")
    tx.begin()
    changed.write_text("partial current run", encoding="utf-8")
    created = reports / "created_latest.json"
    created.write_text('{"status":"partial"}', encoding="utf-8")
    run_dir = reports / "runs" / "failed-run"

    retained = tx.rollback(run_dir)

    assert set(retained) == {"changed_latest.md", "created_latest.json"}
    assert stable.read_text(encoding="utf-8") == "stable"
    assert changed.read_text(encoding="utf-8") == "previous"
    assert not created.exists()
    assert not (run_dir / stable.name).exists()
    assert (run_dir / changed.name).read_text(encoding="utf-8") == "partial current run"
    assert (run_dir / created.name).read_text(encoding="utf-8") == '{"status":"partial"}'
    assert not tx.snapshot_dir.exists()


def test_prune_retains_one_phase_anchor_for_recent_trade_dates(tmp_path):
    reports = tmp_path / "reports"
    for trade_date in ("2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"):
        for phase in ("auction", "intraday", "close"):
            manifest = RunManifest(
                reports,
                f"{trade_date}-{phase}",
                trade_date,
                phase,
            )
            manifest.finish("completed")
            stamp = datetime.fromisoformat(trade_date).timestamp()
            os.utime(manifest.run_dir, (stamp, stamp))

    removed = prune_run_reports(reports, keep=1, anchor_trade_dates=2)
    remaining = {
        path.name for path in (reports / "runs").iterdir() if path.is_dir()
    }

    assert removed == 6
    assert remaining == {
        "2026-07-23-auction",
        "2026-07-23-intraday",
        "2026-07-23-close",
        "2026-07-24-auction",
        "2026-07-24-intraday",
        "2026-07-24-close",
    }
