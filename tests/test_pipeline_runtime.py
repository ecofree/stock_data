import json
from datetime import datetime, timedelta
import os

import pytest

from trade_system.pipeline_runtime import (
    LatestReportTransaction,
    PipelineAlreadyRunning,
    PipelineLock,
    RunManifest,
    reap_stale_run_manifests,
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


def test_reap_stale_running_manifest_marks_parent_and_running_step_aborted(tmp_path):
    reports = tmp_path / "reports"
    manifest = RunManifest(reports, "old-run", "2026-08-11", "auction")
    manifest.upsert_step("collect", "running", ["python", "collect.py"])
    old = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    data = json.loads(manifest.path.read_text(encoding="utf-8"))
    data["started_at"] = old
    manifest.path.write_text(json.dumps(data), encoding="utf-8")

    reaped = reap_stale_run_manifests(reports, max_age_seconds=120)

    assert reaped == ["old-run"]
    updated = json.loads(manifest.path.read_text(encoding="utf-8"))
    assert updated["status"] == "aborted"
    assert updated["steps"][0]["status"] == "aborted"


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


def test_commit_publishes_one_run_pointer_for_latest_artifacts(tmp_path):
    reports = tmp_path / "reports"
    staging = reports / ".staging" / "run-one"
    manifest = RunManifest(reports, "run-one", "2026-08-14", "close")
    tx = LatestReportTransaction(reports, "run-one", staging)
    tx.begin()
    (staging / "daily_review_latest.md").write_text("review", encoding="utf-8")
    (staging / "data_readiness_latest.md").write_text("readiness", encoding="utf-8")
    (staging / "daily_review_latest.lazy.js").write_text(
        "window.__REVIEW_LAZY_DATA__ = {};", encoding="utf-8"
    )
    manifest.finish("completed")

    tx.commit(manifest.run_dir)

    pointer = json.loads(
        (reports / "pipeline_run_latest.json").read_text(encoding="utf-8")
    )
    assert pointer["run_id"] == "run-one"
    assert pointer["trade_date"] == "2026-08-14"
    assert pointer["phase"] == "close"
    assert pointer["run_status"] == "completed"
    assert pointer["artifact_files"] == [
        "daily_review_latest.lazy.js",
        "daily_review_latest.md",
        "data_readiness_latest.md",
    ]
    assert (reports / "data_readiness_current.md").read_text(encoding="utf-8") == "readiness"
    assert (manifest.run_dir / "pipeline_run_latest.json").exists()
    status = json.loads((reports / "pipeline_status_latest.json").read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert (reports / "pipeline_status_latest.html").exists()


def test_failed_run_publishes_status_without_mutating_last_complete_page(tmp_path):
    reports = tmp_path / "reports"
    staging = reports / ".staging" / "failed-run"
    reports.mkdir()
    complete_page = (
        "<!doctype html><html><body><main class=\"wrap\">"
        "<h1>2026-08-27 last complete</h1></main></body></html>"
    )
    (reports / "daily_review_latest.html").write_text(complete_page, encoding="utf-8")
    (reports / "daily_review_last_complete.html").write_text(complete_page, encoding="utf-8")
    manifest = RunManifest(reports, "failed-run", "2026-08-28", "close")
    tx = LatestReportTransaction(reports, "failed-run", staging)
    tx.begin()
    (staging / "partial_latest.md").write_text("partial", encoding="utf-8")
    retained = tx.rollback(manifest.run_dir)
    manifest.finish("failed", "encoding failure")
    tx.publish_failure_status(manifest.run_dir, "encoding failure")

    page = (reports / "daily_review_latest.html").read_text(encoding="utf-8")
    status = json.loads((reports / "pipeline_status_latest.json").read_text(encoding="utf-8"))
    assert retained == ["partial_latest.md"]
    assert page == complete_page
    assert (reports / "daily_review_last_complete.html").read_text(encoding="utf-8") == complete_page
    assert status["status"] == "failed"
    assert status["trade_date"] == "2026-08-28"
    assert "收盘任务执行失败" in (reports / "pipeline_status_latest.html").read_text(encoding="utf-8")


def test_non_close_commit_does_not_publish_review_or_advance_last_complete(tmp_path):
    reports = tmp_path / "reports"
    staging = reports / ".staging" / "intraday-run"
    reports.mkdir()
    complete_page = "<html><body><h1>last close</h1></body></html>"
    (reports / "daily_review_latest.html").write_text(complete_page, encoding="utf-8")
    (reports / "daily_review_last_complete.html").write_text(complete_page, encoding="utf-8")
    manifest = RunManifest(reports, "intraday-run", "2026-08-28", "intraday")
    tx = LatestReportTransaction(reports, "intraday-run", staging)
    tx.begin()
    (staging / "daily_review_latest.html").write_text("wrong intraday page", encoding="utf-8")
    (staging / "data_readiness_latest.md").write_text("intraday readiness", encoding="utf-8")
    manifest.finish("completed")

    tx.commit(manifest.run_dir)

    assert (reports / "daily_review_latest.html").read_text(encoding="utf-8") == complete_page
    assert (reports / "daily_review_last_complete.html").read_text(encoding="utf-8") == complete_page
    pointer = json.loads((reports / "pipeline_run_latest.json").read_text(encoding="utf-8"))
    assert pointer["review_published"] is False
    status_html = (reports / "pipeline_status_latest.html").read_text(encoding="utf-8")
    assert "盘中任务已完成" in status_html
    assert "收盘复盘已发布" not in status_html


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
