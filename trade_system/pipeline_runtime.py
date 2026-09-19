"""Runtime locking and manifests for the bounded collection adapter."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys

from trade_system.file_lock import FileLock, FileLockBusy


STALE_LOCAL_LOCK_GRACE_SECONDS = 120
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _file_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def runtime_fingerprint() -> dict[str, object]:
    """Bind the actual collection dependency closure and declared configuration, never .env values.

    Git failure is unknown, not clean. An explicit work-tree works with the
    retained bare-config repository without changing its shared Git settings.
    """
    errors = []
    git_commit, worktree_dirty = "unknown", None
    git = ["git", "-c", "core.bare=false", "--work-tree=" + str(PROJECT_ROOT)]
    try:
        revision = subprocess.run(git + ["rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                  capture_output=True, text=True, check=False)
        status = subprocess.run(git + ["status", "--porcelain", "--untracked-files=normal"],
                                cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
        if revision.returncode == 0 and len(revision.stdout.strip()) in (40, 64):
            git_commit = revision.stdout.strip()
        else:
            errors.append("git_revision_unavailable")
        if status.returncode == 0:
            worktree_dirty = bool(status.stdout.strip())
        else:
            errors.append("git_worktree_state_unavailable")
    except OSError:
        errors.append("git_unavailable")
    from trade_system.migration_boundary import collection_source_files
    try:
        source_paths = [p for p in collection_source_files(PROJECT_ROOT) if p.suffix == '.py']
    except (OSError, ValueError, SyntaxError):
        source_paths = []
        errors.append('collector_dependency_closure_unavailable')
    config_paths = [PROJECT_ROOT / "trade_system/config.py", PROJECT_ROOT / "pyproject.toml"]
    config_paths += [p for p in (PROJECT_ROOT / "config").rglob("*")
                    if p.is_file() and p.suffix in (".json", ".toml", ".yaml", ".yml")]
    config_paths += list(PROJECT_ROOT.glob("requirements*.lock"))
    schema_paths = [PROJECT_ROOT / "trade_system/schema.py", *sorted((PROJECT_ROOT / "migrations").glob("*.sql"))]
    hashes = {}
    for kind, paths in (("source", source_paths), ("config", config_paths), ("schema", schema_paths)):
        try:
            if not paths:
                raise ValueError("no fingerprint inputs")
            hashes[kind + "_hash"] = _file_fingerprint(paths)
        except (OSError, ValueError):
            errors.append(kind + "_files_unreadable_or_missing")
            hashes[kind + "_hash"] = "unknown"
    try:
        import duckdb
        duckdb_version = str(duckdb.__version__)
    except ImportError:
        duckdb_version = "unknown"
    return {
        "git_commit": git_commit, "worktree_dirty": worktree_dirty,
        **hashes, "fingerprint_complete": not errors, "fingerprint_errors": errors,
        "source_file_count": len(source_paths), "config_file_count": len(config_paths),
        "python_version": platform.python_version(), "python_executable": sys.executable,
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
