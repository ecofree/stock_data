"""Runtime locking, manifests, and report rollback for daily pipelines."""

from __future__ import annotations

from datetime import datetime
import filecmp
import json
import os
from pathlib import Path
import shutil


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
            except (TypeError, ValueError, json.JSONDecodeError):
                existing_pid, age_seconds, existing_payload = 0, 0.0, {}
            # Recover only a demonstrably stale local lock.  A live process or
            # a young malformed lock remains a hard block.
            process_alive = False
            if existing_pid and existing_pid != os.getpid():
                try:
                    os.kill(existing_pid, 0)
                    process_alive = True
                # Windows can surface dead/reused PIDs as SystemError instead
                # of OSError (for example when the scheduler account no
                # longer owns the process).  Any failure to prove liveness is
                # treated as not alive; the age threshold below still protects
                # a young lock from accidental recovery.
                except Exception:
                    process_alive = False
            if not process_alive and age_seconds > 6 * 3600:
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

    def finish(self, status: str, error: str | None = None) -> None:
        self.data["status"] = status
        self.data["completed_at"] = datetime.now().isoformat(timespec="seconds")
        if error:
            self.data["error"] = error
        self.write()

    def write(self) -> None:
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


class LatestReportTransaction:
    """Restore root latest reports if a pipeline fails midway."""

    def __init__(self, reports_dir: str | Path, run_id: str) -> None:
        self.reports_dir = Path(reports_dir).resolve()
        self.snapshot_dir = self.reports_dir / f".rollback_{run_id}"
        self.original_names: set[str] = set()

    def begin(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
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
            for path in self.reports_dir.glob("*latest*"):
                if not path.is_file() or not self._run_artifact(path):
                    continue
                snapshot = self.snapshot_dir / path.name
                changed = not snapshot.exists() or not filecmp.cmp(path, snapshot, shallow=False)
                if changed:
                    shutil.copy2(path, run_path / path.name)
                    retained.append(path.name)
        for path in self.reports_dir.glob("*latest*"):
            if path.is_file() and path.name not in self.original_names:
                path.unlink()
        for path in self.snapshot_dir.iterdir():
            if path.is_file():
                shutil.copy2(path, self.reports_dir / path.name)
        shutil.rmtree(self.snapshot_dir)
        return retained

    def commit(self, run_dir: str | Path) -> None:
        run_path = Path(run_dir)
        for path in self.reports_dir.glob("*latest*"):
            if path.is_file():
                # Large model diagnostics are current-state artifacts, not
                # immutable per-run evidence.  Copying them into every 5-minute
                # run caused unbounded report growth.
                if not self._run_artifact(path):
                    continue
                shutil.copy2(path, run_path / path.name)
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
