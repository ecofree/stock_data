"""Runtime locking, manifests, and report rollback for daily pipelines."""

from __future__ import annotations

from datetime import datetime
import filecmp
import json
import os
from pathlib import Path
import shutil

try:
    import psutil
except Exception:  # pragma: no cover - optional on minimal runtimes
    psutil = None


STALE_LOCAL_LOCK_GRACE_SECONDS = 120


class PipelineAlreadyRunning(RuntimeError):
    pass


class PipelineLock:
    def __init__(self, db_path: str | Path, run_id: str) -> None:
        db = Path(db_path).resolve()
        self.path = db.with_name(f"{db.name}.pipeline.lock")
        self.run_id = run_id

    def __enter__(self) -> "PipelineLock":
        payload = json.dumps(
            {
                "run_id": self.run_id,
                "pid": os.getpid(),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown",
            },
            ensure_ascii=False,
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing_text = self.path.read_text(encoding="utf-8", errors="replace")[:500]
            try:
                existing_payload = json.loads(existing_text)
                existing_pid = int(existing_payload.get("pid") or 0)
                started_at = datetime.fromisoformat(str(existing_payload.get("started_at")))
                age_seconds = max(0.0, (datetime.now() - started_at).total_seconds())
            except (TypeError, ValueError, json.JSONDecodeError, OSError):
                existing_pid, existing_payload = 0, {}
                # A truncated lock from a hard power loss is still recoverable
                # once its filesystem mtime is older than the local grace
                # period.  Treating malformed content as age zero otherwise
                # leaves the scheduler blocked forever.
                try:
                    age_seconds = max(
                        0.0, (datetime.now() - datetime.fromtimestamp(self.path.stat().st_mtime)).total_seconds()
                    )
                except OSError:
                    age_seconds = 0.0
            # Recover a demonstrably stale local lock.  A process that died
            # during a reboot/provider crash must not block the next market
            # phase for six hours; the short grace period protects a just-
            # created lock from racing with the owner process.
            process_alive = False
            if existing_pid and existing_pid != os.getpid():
                try:
                    if psutil is not None:
                        process = psutil.Process(existing_pid)
                        process_started = datetime.fromtimestamp(process.create_time())
                        process_alive = process.is_running() and process_started <= started_at
                    else:
                        os.kill(existing_pid, 0)
                        process_alive = True
                # Windows can surface dead/reused PIDs as SystemError instead
                # of OSError (for example when the scheduler account no
                # longer owns the process).  Any failure to prove liveness is
                # treated as not alive; the age threshold below still protects
                # a young lock from accidental recovery.
                except Exception:
                    process_alive = False
            current_host = str(os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown")
            lock_host = str(existing_payload.get("host") or "")
            recovery_age = (
                STALE_LOCAL_LOCK_GRACE_SECONDS
                if lock_host in {"", current_host, "unknown"}
                else 6 * 3600
            )
            if (
                not process_alive
                and age_seconds > recovery_age
            ):
                try:
                    self.path.unlink()
                    return self.__enter__()
                except FileNotFoundError:
                    return self.__enter__()
            raise PipelineAlreadyRunning(
                f"Pipeline lock already exists at {self.path}: {existing_text}"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if payload.get("run_id") == self.run_id:
            self.path.unlink()


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
        for path in self.reports_dir.glob("*latest*"):
            if path.is_file():
                self.original_names.add(path.name)
                shutil.copy2(path, self.snapshot_dir / path.name)

    @staticmethod
    def _run_artifact(path: Path) -> bool:
        return path.name not in {"qlib_shadow_training_latest.json", "qlib_features_latest.csv"}

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
        for path in self.reports_dir.glob("*latest*"):
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
        source_paths = self.staging_dir.glob("*latest*") if self.staging_dir != self.reports_dir else self.reports_dir.glob("*latest*")
        published_names: list[str] = []
        for path in source_paths:
            if path.is_file():
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
        # One small, machine-readable pointer makes the root ``*_latest``
        # files auditable as a set.  The pointer itself is included in the
        # transaction snapshot because its name also contains ``latest``.
        manifest_path = run_path / "run.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
        }
        pointer_path = self.reports_dir / "pipeline_run_latest.json"
        pointer_temp = pointer_path.with_suffix(pointer_path.suffix + ".publish.tmp")
        pointer_temp.write_text(json.dumps(pointer, ensure_ascii=False, indent=2), encoding="utf-8")
        pointer_temp.replace(pointer_path)
        shutil.copy2(pointer_path, run_path / pointer_path.name)
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
