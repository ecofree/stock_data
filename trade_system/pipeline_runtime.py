"""Runtime locking and manifests for the bounded collection adapter."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

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


def all_manifests(reports_dir: str | Path) -> list[dict]:
    """Retain every receipt. A later green run must not erase a missed slot."""
    root = Path(reports_dir).resolve() / "runs"
    out: list[dict] = []
    if not root.exists():
        return out
    for path in root.glob("*/run.json"):
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            raw = path.read_bytes()
            item = json.loads(raw.decode("utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        trade_date = str(item.get("trade_date") or "")[:10]
        phase = str(item.get("phase") or "").lower()
        if not trade_date or phase not in ("auction", "intraday", "close", "supplemental"):
            continue
        item["_run_dir"] = str(path.parent)
        item["_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        out.append(item)
    return out


def latest_manifests(reports_dir: str | Path) -> dict[tuple[str, str], dict]:
    """Presentation helper only; complete-day acceptance uses all receipts."""
    out: dict[tuple[str, str], dict] = {}
    for item in all_manifests(reports_dir):
        key = (str(item["trade_date"])[:10], str(item["phase"]).lower())
        stamp = str(item.get("completed_at") or item.get("started_at") or "")
        old_stamp = str(
            out.get(key, {}).get("completed_at")
            or out.get(key, {}).get("started_at")
            or ""
        )
        if key not in out or stamp >= old_stamp:
            out[key] = item
    return out


def default_observation_policy() -> dict:
    """Declare the existing full-day task cadence only when sealing a new contract.

    This is never an acceptance fallback for an old undeclared contract.
    """
    def segment(first, last, interval, tolerance, budget):
        return {"first_start": first, "last_start": last, "interval_seconds": interval,
                "start_tolerance_seconds": tolerance, "completion_budget_seconds": budget}
    return {"schema": 1, "timezone": "Asia/Shanghai", "day_rule": "all_required_windows",
            "phases": {
                "auction": [segment("09:16:00", "09:24:00", 120, 30, 90)],
                "intraday": [segment("09:30:00", "11:25:00", 300, 30, 240),
                             segment("13:00:00", "15:00:00", 300, 30, 240)],
                "close": [segment("17:30:00", "17:30:00", 0, 360, 3600)]}}


def observation_windows(policy: dict, trade_date: str, phase: str) -> list[dict]:
    """Expand the accepted, hash-bound schedule without inventing legacy slots.

    Start tolerance accounts for local task launch overhead. The completion
    deadline stays anchored to the slot, so retries cannot reset its budget.
    Lunch is not an observation slot. Trading-day verification is performed
    separately by the caller against the real exchange calendar.
    """
    if (not isinstance(policy, dict) or type(policy.get("schema")) is not int or policy.get("schema") != 1
            or policy.get("timezone") != "Asia/Shanghai"
            or policy.get("day_rule") != "all_required_windows"
            or not isinstance(policy.get("phases"), dict)
            or set(policy["phases"]) != {"auction", "intraday", "close"}):
        raise ValueError("observation window contract missing or unsupported")
    if phase not in {"auction", "intraday", "close"}:
        raise ValueError("unsupported observation phase")
    day = date.fromisoformat(trade_date)
    segments = policy["phases"][phase]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 4:
        raise ValueError("observation segments missing or excessive")
    budget_limit = {"auction": 90, "intraday": 240, "close": 3600}[phase]
    tolerance_limit = 360 if phase == "close" else 30
    windows = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("invalid observation segment")
        clocks = []
        for name in ("first_start", "last_start"):
            text = segment.get(name)
            if not isinstance(text, str) or len(text) != 8:
                raise ValueError("observation clock must be HH:MM:SS")
            try:
                clock = datetime.strptime(text, "%H:%M:%S").time()
            except ValueError as exc:
                raise ValueError("invalid observation clock") from exc
            if clock.strftime("%H:%M:%S") != text:
                raise ValueError("noncanonical observation clock")
            clocks.append(datetime.combine(day, clock, ZoneInfo("Asia/Shanghai")))
        first, last = clocks
        numbers = {}
        for name in ("interval_seconds", "start_tolerance_seconds", "completion_budget_seconds"):
            value = segment.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("observation budgets must be explicit integers")
            numbers[name] = value
        interval, tolerance, budget = (numbers[n] for n in
            ("interval_seconds", "start_tolerance_seconds", "completion_budget_seconds"))
        if (last < first or not 0 <= tolerance <= tolerance_limit
                or not 1 <= budget <= budget_limit or not 0 <= interval <= 86400
                or (interval == 0 and first != last)
                or (interval and (interval < 30 or (last-first).total_seconds() % interval
                                  or tolerance + budget > interval))):
            raise ValueError("observation interval or budget invalid")
        current = first
        while current <= last:
            clock = current.time()
            deadline = current + timedelta(seconds=tolerance + budget)
            legal = (
                phase == "auction" and time(9, 15) <= clock < time(9, 30)
                and deadline.time() <= time(9, 30)
                or phase == "intraday" and (
                    time(9, 30) <= clock < time(11, 30) and deadline.time() <= time(11, 30)
                    or time(13) <= clock <= time(15) and deadline.time() <= time(15, 5))
                or phase == "close" and clock >= time(15, 5)
                and deadline.date() == day
            )
            if not legal or len(windows) >= 200:
                raise ValueError("observation slot outside market phase or excessive")
            windows.append({
                "window_id": phase + ":" + current.strftime("%H:%M:%S"),
                "phase": phase, "start_at": current.isoformat(),
                "start_latest_at": (current + timedelta(seconds=tolerance)).isoformat(),
                "deadline_at": deadline.isoformat(),
                "deadline_epoch": deadline.timestamp(), "interval_seconds": interval,
                "completion_budget_seconds": budget,
            })
            if interval == 0:
                break
            current += timedelta(seconds=interval)
    windows.sort(key=lambda item: item["start_at"])
    for previous, current in zip(windows, windows[1:]):
        if previous["deadline_at"] > current["start_at"]:
            raise ValueError("duplicate or overlapping observation slots")
    return windows


def load_observation_contract(path: str | Path, expected_sha256: str) -> dict:
    """Verify the original accepted bytes; run self-reported policy is not proof."""
    expected = str(expected_sha256).lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("explicit collector contract SHA256 required")
    source = Path(path).resolve()
    if source.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("collector contract exceeds read budget")
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("collector contract SHA256 mismatch")
    contract = json.loads(raw.decode("utf-8-sig"))
    policy = contract.get("observation_windows") if isinstance(contract, dict) else None
    for phase in ("auction", "intraday", "close"):
        observation_windows(policy, "2000-01-04", phase)
    return policy


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

    def bind_observation_contract(self, path: str | Path, expected_sha256: str) -> None:
        policy = load_observation_contract(path, expected_sha256)
        self.data["observation_contract_path"] = str(Path(path).resolve())
        self.data["observation_contract_sha256"] = expected_sha256.lower()
        self.data["observation_windows"] = policy
        window_id = os.environ.get("STOCKDATA_OBSERVATION_WINDOW_ID")
        if window_id:
            self.data["observation_window_id"] = window_id
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
