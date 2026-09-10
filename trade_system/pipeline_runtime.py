"""Runtime locking, manifests, and report rollback for daily pipelines."""

from __future__ import annotations

from datetime import datetime
import filecmp
import hashlib
import html
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys

from trade_system.file_lock import FileLock, FileLockBusy


STALE_LOCAL_LOCK_GRACE_SECONDS = 120
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _file_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item).lower()):
        try:
            digest.update(str(path.relative_to(PROJECT_ROOT)).encode("utf-8"))
            digest.update(path.read_bytes())
        except (OSError, ValueError):
            continue
    return digest.hexdigest()


def runtime_fingerprint() -> dict[str, str | bool]:
    """Capture non-secret inputs that determine a run's interpretation.

    The fingerprint is deliberately small and source-bound: it records the
    repository revision/dirty state plus the files that define schema,
    configuration and publication behavior.  It never serializes credentials
    or environment values.
    """
    git_commit = "unknown"
    worktree_dirty = False
    try:
        git_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False,
        ).stdout.strip() or "unknown"
        worktree_dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False,
        ).stdout.strip())
    except OSError:
        pass
    schema_paths = [PROJECT_ROOT / "trade_system" / "schema.py"]
    schema_paths.extend((PROJECT_ROOT / "migrations").glob("*.sql"))
    config_paths = [PROJECT_ROOT / "trade_system" / "config.py"]
    source_paths = [
        PROJECT_ROOT / "trade_system" / "pipeline_runtime.py",
        PROJECT_ROOT / "scripts" / "run_integrated_daily.py",
    ]
    try:
        import duckdb
        duckdb_version = str(duckdb.__version__)
    except Exception:
        duckdb_version = "unknown"
    return {
        "git_commit": git_commit,
        "worktree_dirty": worktree_dirty,
        "source_hash": _file_fingerprint(source_paths),
        "config_hash": _file_fingerprint(config_paths),
        "schema_hash": _file_fingerprint(schema_paths),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "duckdb_version": duckdb_version,
    }


class PipelineAlreadyRunning(RuntimeError):
    pass


class PipelineLock:
    def __init__(self, db_path: str | Path, run_id: str) -> None:
        db = Path(db_path).resolve()
        self.path = db.with_name(f"{db.name}.pipeline.lock")
        self.run_id = run_id
        self._guard = FileLock(self.path.with_suffix(self.path.suffix + '.guard'))

    def __enter__(self) -> "PipelineLock":
        try:
            self._guard.__enter__()
        except FileLockBusy as exc:
            raise PipelineAlreadyRunning(str(exc)) from exc
        try:
            if self.path.exists():
                try:
                    previous = json.loads(self.path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    previous = {}
                if not isinstance(previous, dict) or previous.get('lock_protocol') != 'os_handle_v2':
                    raise PipelineAlreadyRunning(
                        f"Legacy/unknown lock requires coordinated maintenance: {self.path}"
                    )
            return self._write_owner()
        except BaseException:
            self._guard.__exit__(None, None, None)
            raise

    def _write_owner(self) -> "PipelineLock":
        payload = json.dumps(
            {
                "lock_protocol": "os_handle_v2",
                "run_id": self.run_id,
                "pid": os.getpid(),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown",
            },
            ensure_ascii=False,
        )
        # Write/replace metadata only after the permanent guard is held.
        temp = self.path.with_suffix(self.path.suffix + '.tmp')
        with temp.open('w', encoding='utf-8') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(self.path)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._guard.fd is None:
            return
        try:
            if self.path.exists():
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if payload.get("run_id") == self.run_id:
                    self.path.unlink()
        finally:
            self._guard.__exit__(exc_type, exc, tb)


class RunManifest:
    def __init__(
        self,
        reports_dir: str | Path,
        run_id: str,
        trade_date: str,
        phase: str | None = None,
    ) -> None:
        self.run_dir = Path(reports_dir).resolve() / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "run.json"
        self.data = {
            "run_id": run_id,
            "trade_date": trade_date,
            "phase": phase,
            "status": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "completed_at": None,
            "steps": [],
            "runtime_fingerprint": runtime_fingerprint(),
        }
        self.write()

    def add_step(self, name: str, status: str, command: list[str], **extra) -> None:
        item = {"name": name, "status": status, "command": command, **extra}
        self.data["steps"].append(item)
        self.write()

    def upsert_step(self, name: str, status: str, command: list[str], **extra) -> None:
        """Insert a step or update it in place (matched by name) so a step transitions
        running -> completed/failed/degraded as a single entry rather than duplicate
        rows.  Writing the 'running' state up front is what makes a hung or externally
        killed step visible (A5)."""
        for item in self.data["steps"]:
            if item.get("name") == name:
                item.update({"status": status, "command": command, **extra})
                self.write()
                return
        self.data["steps"].append({"name": name, "status": status, "command": command, **extra})
        self.write()

    def finish(
        self,
        status: str,
        error: str | None = None,
        *,
        warnings: list[str] | None = None,
    ) -> None:
        self.data["status"] = status
        self.data["completed_at"] = datetime.now().isoformat(timespec="seconds")
        if error:
            self.data["error"] = error
        if warnings:
            self.data["warnings"] = list(warnings)
        self.write()

    def write(self) -> None:
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


def reap_stale_run_manifests(
    reports_dir: str | Path,
    *,
    max_age_seconds: int = STALE_LOCAL_LOCK_GRACE_SECONDS,
    exclude_run_id: str | None = None,
) -> list[str]:
    """Mark abandoned ``running`` manifests as aborted after a restart.

    The pipeline lock is acquired before this helper is called, so the current
    run is protected and no other phase can be live against the same database.
    Historical ``running`` JSON files otherwise survive a reboot forever and
    make the P0 observation report a false active run.
    """
    root = Path(reports_dir).resolve() / "runs"
    if not root.exists():
        return []
    # Direct/manual callers must not reap a live run.  The production runner
    # passes ``exclude_run_id`` only after acquiring the database lock; without
    # that proof, an active ``*.duckdb.pipeline.lock`` makes this a no-op.
    if exclude_run_id is None and any(root.parent.glob("*.duckdb.pipeline.lock")):
        return []
    now = datetime.now()
    reaped: list[str] = []
    for path in root.glob("*/run.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") != "running" or data.get("run_id") == exclude_run_id:
                continue
            started = datetime.fromisoformat(str(data.get("started_at")))
            if (now - started).total_seconds() <= max_age_seconds:
                continue
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        data["status"] = "aborted"
        data["completed_at"] = now.isoformat(timespec="seconds")
        data["error"] = "stale_running_manifest_reaped_after_seconds"
        for step in data.get("steps") or []:
            if step.get("status") == "running":
                step["status"] = "aborted"
                step["reason"] = "parent_run_manifest_reaped"
        temp = path.with_suffix(".json.tmp")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
            reaped.append(str(data.get("run_id") or path.parent.name))
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass
    return reaped


class LatestReportTransaction:
    """Publish a completed run's latest reports atomically.

    Generators are pointed at ``staging_dir`` by the integrated runner.  Root
    ``*_latest`` files therefore remain coherent while a long close run is in
    progress and are only replaced with temp-file swaps after the manifest has
    reached its final status.
    """

    def __init__(self, reports_dir: str | Path, run_id: str, staging_dir: str | Path | None = None) -> None:
        if (Path(reports_dir)/'v2-publication-owner.json').exists():
            raise ValueError('legacy publisher cannot write the V2 publication namespace')
        self.reports_dir = Path(reports_dir).resolve()
        self.snapshot_dir = self.reports_dir / f".rollback_{run_id}"
        self.staging_dir = Path(staging_dir).resolve() if staging_dir else self.reports_dir
        self.original_names: set[str] = set()

    def begin(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        if self.staging_dir != self.reports_dir and self.staging_dir.exists():
            # A reused explicit run-id may leave a partial staging directory
            # after a power loss.  It is not a published artifact and is safe
            # to replace before taking the new root snapshot.
            shutil.rmtree(self.staging_dir)
        if self.staging_dir != self.reports_dir:
            self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=False)
        snapshot_paths = list(self.reports_dir.glob("*latest*"))
        last_complete = self.reports_dir / "daily_review_last_complete.html"
        if last_complete.is_file():
            snapshot_paths.append(last_complete)
        for path in snapshot_paths:
            if path.is_file():
                self.original_names.add(path.name)
                shutil.copy2(path, self.snapshot_dir / path.name)

    @staticmethod
    def _run_artifact(path: Path) -> bool:
        return path.name not in {"qlib_shadow_training_latest.json", "qlib_features_latest.csv"}

    # These names predate the run-scoped ``*_latest`` publication pointer and
    # are still used by operators and older dashboard links. Keep them as
    # atomically refreshed aliases so they cannot silently lag the published
    # report set.
    CURRENT_ALIASES = {
        "data_readiness_latest.md": "data_readiness_current.md",
        "p0_p3_acceptance_latest.md": "p0_p3_acceptance_current.md",
        "daily_review_artifact_audit_latest.md": "daily_review_artifact_audit_current.md",
    }

    def _write_pipeline_status(
        self,
        run_path: Path,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        manifest_path = run_path / "run.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload = {
            "run_id": manifest.get("run_id"),
            "trade_date": manifest.get("trade_date"),
            "phase": manifest.get("phase"),
            "status": status,
            "started_at": manifest.get("started_at"),
            "completed_at": manifest.get("completed_at"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "error": error,
            "last_complete_report": "daily_review_last_complete.html"
            if (self.reports_dir / "daily_review_last_complete.html").exists()
            else None,
            "review_published": bool(
                str(manifest.get("phase") or "") == "close"
                and (run_path / "daily_review_latest.html").is_file()
            ),
        }
        json_path = self.reports_dir / "pipeline_status_latest.json"
        json_temp = json_path.with_suffix(json_path.suffix + ".publish.tmp")
        json_temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        json_temp.replace(json_path)

        failed = status == "failed"
        phase = str(payload.get("phase") or "unknown")
        color = "#d64545" if failed else "#2f9e6f"
        phase_labels = {
            "auction": "竞价",
            "intraday": "盘中",
            "close": "收盘",
            "history": "历史",
        }
        phase_label = phase_labels.get(phase, phase)
        if failed:
            title = f"{phase_label}任务执行失败"
        elif phase == "close" and payload["review_published"]:
            title = "今日收盘复盘已发布"
        elif phase == "close":
            title = "今日收盘任务已完成（复盘未发布）"
        else:
            title = f"{phase_label}任务已完成"
        detail = html.escape(error or "运行已完成")
        trade_date = html.escape(str(payload.get("trade_date") or "—"))
        run_id = html.escape(str(payload.get("run_id") or "—"))
        link = (
            "<a href='daily_review_last_complete.html'>打开最后完整复盘</a>"
            if payload.get("last_complete_report") and failed
            else (
                "<a href='daily_review_latest.html'>打开最新复盘</a>"
                if payload.get("review_published")
                else "<a href='pipeline_run_latest.json'>查看本次运行清单</a>"
            )
        )
        status_html = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title} · {trade_date}</title><style>
body{{margin:0;background:#11100d;color:#eee4d3;font-family:system-ui,'Microsoft YaHei',sans-serif}}
main{{max-width:960px;margin:8vh auto;padding:32px}}.card{{border:1px solid {color};border-radius:12px;padding:24px;background:#1b1713}}
h1{{margin-top:0}}code{{color:#e5b96f}}a{{color:#7fb2ff}}.dim{{color:#a89880}}
</style></head><body><main><div class='card'><h1>{title}</h1>
<p>交易日：<code>{trade_date}</code>　运行：<code>{run_id}</code></p>
<p>{detail}</p><p>{link}</p><p class='dim'>该页面由流水线状态发布器静态生成，不依赖运行时取数。</p>
</div></main></body></html>"""
        html_path = self.reports_dir / "pipeline_status_latest.html"
        html_temp = html_path.with_suffix(html_path.suffix + ".publish.tmp")
        html_temp.write_text(status_html, encoding="utf-8")
        html_temp.replace(html_path)
        shutil.copy2(json_path, run_path / json_path.name)
        shutil.copy2(html_path, run_path / html_path.name)

    def publish_failure_status(self, run_dir: str | Path, error: str) -> None:
        """Publish failure status while leaving the last published review untouched."""
        run_path = Path(run_dir)
        run_path.mkdir(parents=True, exist_ok=True)
        self._write_pipeline_status(run_path, status="failed", error=error)

    def rollback(self, run_dir: str | Path | None = None) -> list[str]:
        """Restore root latest reports and retain changed failure artifacts.

        Root ``latest`` files must continue to mean the last fully committed
        run.  At the same time, partial reports from a failed run are essential
        forensic evidence.  Copy only files created or changed by this
        transaction into the immutable run directory before restoring the
        previous root state.
        """
        retained: list[str] = []
        run_path = Path(run_dir) if run_dir is not None else None
        if run_path is not None:
            run_path.mkdir(parents=True, exist_ok=True)
            staged = self.staging_dir != self.reports_dir
            source_paths = self.staging_dir.glob("*latest*") if staged else self.reports_dir.glob("*latest*")
            for path in source_paths:
                if not path.is_file() or not self._run_artifact(path):
                    continue
                if not staged:
                    snapshot = self.snapshot_dir / path.name
                    if snapshot.exists() and filecmp.cmp(path, snapshot, shallow=False):
                        continue
                shutil.copy2(path, run_path / path.name)
                retained.append(path.name)
        if self.staging_dir != self.reports_dir and self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)
        managed_paths = list(self.reports_dir.glob("*latest*"))
        managed_paths.append(self.reports_dir / "daily_review_last_complete.html")
        for path in managed_paths:
            if path.is_file() and path.name not in self.original_names:
                path.unlink()
        for path in self.snapshot_dir.iterdir():
            if path.is_file():
                shutil.copy2(path, self.reports_dir / path.name)
        shutil.rmtree(self.snapshot_dir)
        return retained

    def cleanup_staging(self) -> None:
        """Remove an unpublished staging tree after a pre-commit failure."""
        if self.staging_dir != self.reports_dir and self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)

    def commit(self, run_dir: str | Path) -> None:
        run_path = Path(run_dir)
        manifest = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        phase = str(manifest.get("phase") or "")
        source_paths = self.staging_dir.glob("*latest*") if self.staging_dir != self.reports_dir else self.reports_dir.glob("*latest*")
        published_names: list[str] = []
        for path in source_paths:
            if path.is_file():
                # Auction/intraday runs must never replace a close review or
                # its inline lazy payload.  Their status and run manifest are
                # the publication surface for that phase.
                if phase != "close" and path.name.startswith("daily_review_latest"):
                    continue
                # Large model diagnostics are current-state artifacts, not
                # immutable per-run evidence.  Copying them into every 5-minute
                # run caused unbounded report growth.
                if not self._run_artifact(path):
                    continue
                shutil.copy2(path, run_path / path.name)
                target = self.reports_dir / path.name
                temp = target.with_suffix(target.suffix + ".publish.tmp")
                shutil.copy2(path, temp)
                temp.replace(target)
                published_names.append(path.name)
        for latest_name, current_name in self.CURRENT_ALIASES.items():
            source = self.reports_dir / latest_name
            if not source.is_file():
                continue
            target = self.reports_dir / current_name
            temp = target.with_suffix(target.suffix + ".publish.tmp")
            shutil.copy2(source, temp)
            temp.replace(target)
        # One small, machine-readable pointer makes the root ``*_latest``
        # files auditable as a set.  The pointer itself is included in the
        # transaction snapshot because its name also contains ``latest``.
        pointer = {
            "run_id": manifest.get("run_id"),
            "trade_date": manifest.get("trade_date"),
            "phase": manifest.get("phase"),
            "run_status": manifest.get("status"),
            "started_at": manifest.get("started_at"),
            "completed_at": manifest.get("completed_at"),
            "published_at": datetime.now().isoformat(timespec="seconds"),
            "run_dir": str(run_path.relative_to(self.reports_dir)),
            "artifact_files": sorted(published_names),
            "review_published": bool(
                phase == "close" and "daily_review_latest.html" in published_names
            ),
        }
        pointer_path = self.reports_dir / "pipeline_run_latest.json"
        pointer_temp = pointer_path.with_suffix(pointer_path.suffix + ".publish.tmp")
        pointer_temp.write_text(json.dumps(pointer, ensure_ascii=False, indent=2), encoding="utf-8")
        pointer_temp.replace(pointer_path)
        shutil.copy2(pointer_path, run_path / pointer_path.name)
        current_review = self.reports_dir / "daily_review_latest.html"
        review_published = bool(
            phase == "close"
            and "daily_review_latest.html" in published_names
            and current_review.exists()
            and "pipeline-failure-banner" not in current_review.read_text(encoding="utf-8", errors="replace")
        )
        if review_published:
            last_complete = self.reports_dir / "daily_review_last_complete.html"
            last_complete_temp = last_complete.with_suffix(last_complete.suffix + ".publish.tmp")
            shutil.copy2(current_review, last_complete_temp)
            last_complete_temp.replace(last_complete)
            shutil.copy2(last_complete, run_path / last_complete.name)
        self._write_pipeline_status(run_path, status=str(manifest.get("status") or "completed"))
        if self.staging_dir != self.reports_dir and self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)
        shutil.rmtree(self.snapshot_dir)


def prune_run_reports(
    reports_dir: str | Path,
    *,
    keep: int = 30,
    anchor_trade_dates: int = 10,
) -> int:
    """Bound scheduler storage while retaining one daily phase anchor.

    Auction and intraday watchers can create more than 80 successful runs in a
    day.  Keeping only the latest 30 directories erased the close/auction
    evidence needed by the five-session observation window.  Preserve the
    newest run for each phase on the latest bounded set of trade dates, plus
    the normal most-recent run budget.
    """
    root = Path(reports_dir).resolve() / "runs"
    if not root.exists():
        return 0
    run_dirs = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    metadata: list[tuple[Path, str, str]] = []
    for path in run_dirs:
        try:
            payload = json.loads((path / "run.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        trade_date = str(payload.get("trade_date") or "")[:10]
        phase = str(payload.get("phase") or "").lower()
        if trade_date and phase:
            metadata.append((path, trade_date, phase))
    recent_dates = set(
        sorted({item[1] for item in metadata}, reverse=True)[
            : max(1, int(anchor_trade_dates))
        ]
    )
    anchors: dict[tuple[str, str], Path] = {}
    # ``run_dirs`` is newest-first, so setdefault selects the latest phase run.
    for path, trade_date, phase in metadata:
        if trade_date in recent_dates:
            anchors.setdefault((trade_date, phase), path)
    preserve = set(run_dirs[: max(1, int(keep))]) | set(anchors.values())
    removed = 0
    for path in run_dirs:
        if path in preserve:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed
