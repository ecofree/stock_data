"""Runtime locking and manifests for the bounded collection adapter."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import gzip
import json
import math
import os
import platform
from pathlib import Path
import subprocess
import sys
import time as clock
import uuid
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
        self.recovered_owner = None

    def __enter__(self) -> "PipelineLock":
        try:
            self._guard.__enter__()
        except FileLockBusy as exc:
            raise PipelineAlreadyRunning(str(exc)) from exc
        try:
            if self.path.exists():
                try:
                    raw = self.path.read_bytes()
                    if len(raw) > 256_000:
                        raise ValueError('owner metadata exceeds read budget')
                    previous = json.loads(raw.decode('utf-8'))
                except (OSError, ValueError):
                    previous = {}
                if not isinstance(previous, dict) or previous.get('lock_protocol') != 'os_handle_v2':
                    raise PipelineAlreadyRunning(
                        f"Legacy/unknown lock requires coordinated maintenance: {self.path}"
                    )
                # The acquired OS handle, never a PID or elapsed age, proves
                # that this v2 owner is no longer holding the shared guard.
                self.recovered_owner = {key: previous.get(key) for key in
                    ('run_id', 'pid', 'started_at', 'host', 'lock_protocol')}
                self.recovered_owner['metadata_sha256'] = hashlib.sha256(raw).hexdigest()
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


def _before_deadline(deadline_epoch):
    if deadline_epoch is not None:
        if not math.isfinite(deadline_epoch):
            raise ValueError('finite backup deadline required')
        if clock.time() >= deadline_epoch:
            raise TimeoutError('backup deadline exhausted; incomplete files are not qualified')


def _runtime_json(path: Path, value):
    """Commit evidence atomically; never expose an incomplete qualification."""
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def _stream_digest(stream, deadline_epoch=None):
    digest, size = hashlib.sha256(), 0
    while True:
        _before_deadline(deadline_epoch)
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return digest.hexdigest(), size
        digest.update(chunk)
        size += len(chunk)


def prepare_daily_backup(db_path, backup_dir, run_id, *, deadline_epoch=None):
    """Copy a closed database under the same owner protocol as collection.

    Deadline checks are cooperative between I/O chunks. A blocked filesystem
    call is not forcibly interrupted; the supervising runner exposes its owner.
    """
    import duckdb
    db = Path(db_path).resolve(strict=True)
    folder = Path(backup_dir).resolve()
    if not run_id or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for ch in run_id):
        raise ValueError('safe backup run id required')
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / ('kpl_data_pre_daily_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + run_id + '.duckdb')
    partial = Path(str(raw) + '.partial')
    with PipelineLock(db, 'backup_' + run_id) as owner:
        # Even an interrupted or failed copy retains the prior-owner recovery
        # boundary. This receipt cannot qualify a restoration point.
        _runtime_json(Path(str(raw) + '.raw.json'), {'schema': 1, 'status': 'preparing',
            'raw': str(raw), 'source_database': str(db), 'run_id': run_id,
            'prepared_at': datetime.now().isoformat(), 'recovered_owner': owner.recovered_owner,
            'qualified_recovery_point': False})
        _before_deadline(deadline_epoch)
        wal = Path(str(db) + '.wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('nonempty source WAL requires coordinated backup; no incomplete database copy')
        # Read-only DuckDB ownership also refuses an incompatible live writer.
        with duckdb.connect(str(db), read_only=True) as source:
            tables = source.execute('SELECT table_schema,table_name FROM information_schema.tables ORDER BY 1,2').fetchall()
            before = db.stat()
            digest, size = hashlib.sha256(), 0
            with db.open('rb') as src, partial.open('xb') as dst:
                while True:
                    _before_deadline(deadline_epoch)
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            after = db.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
                raise ValueError('source changed during guarded copy; raw remains unqualified')
        _before_deadline(deadline_epoch)
        with duckdb.connect(str(partial), read_only=True) as copy:
            copied_tables = copy.execute('SELECT table_schema,table_name FROM information_schema.tables ORDER BY 1,2').fetchall()
            if copied_tables != tables:
                raise ValueError('copied database catalogue differs')
        partial.replace(raw)
        receipt = {'schema': 1, 'status': 'raw_verified_not_compressed', 'raw': str(raw),
            'raw_sha256': digest.hexdigest(), 'raw_size': size, 'source_database': str(db),
            'source_size': before.st_size, 'source_mtime_ns': before.st_mtime_ns,
            'catalogue': tables, 'prepared_at': datetime.now().isoformat(),
            'recovered_owner': owner.recovered_owner,
            'deadline_is_cooperative': True, 'qualified_recovery_point': False}
        _runtime_json(Path(str(raw) + '.raw.json'), receipt)
        return receipt


def finalize_daily_backup(raw_path, *, deadline_epoch=None):
    """Qualify only a gzip whose complete decompressed bytes match the raw."""
    raw = Path(raw_path).resolve(strict=True)
    receipt = json.loads(Path(str(raw) + '.raw.json').read_text(encoding='utf-8'))
    if receipt.get('schema') != 1 or receipt.get('status') != 'raw_verified_not_compressed' or receipt.get('raw') != str(raw):
        raise ValueError('bound verified raw receipt required')
    archive = Path(str(raw) + '.gz')
    qualified = Path(str(archive) + '.qualified.json')
    if archive.exists() or qualified.exists():
        raise ValueError('new final archive required; retained recovery evidence cannot be overwritten')
    partial = Path(str(archive) + '.partial')
    # Keep the verified raw on every failure, including qualification writes.
    with raw.open('rb') as src, partial.open('xb') as dst:
        digest, size = hashlib.sha256(), 0
        with gzip.GzipFile(fileobj=dst, mode='wb', mtime=0) as compressed:
            while True:
                _before_deadline(deadline_epoch)
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                compressed.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    if digest.hexdigest() != receipt['raw_sha256'] or size != receipt['raw_size']:
        raise ValueError('verified raw changed before compression; archive unqualified')
    with gzip.open(partial, 'rb') as decompressed:
        restored_sha, restored_size = _stream_digest(decompressed, deadline_epoch)
    if restored_sha != receipt['raw_sha256'] or restored_size != receipt['raw_size']:
        raise ValueError('compressed roundtrip differs; raw retained')
    with partial.open('rb') as stream:
        archive_sha, archive_size = _stream_digest(stream, deadline_epoch)
    partial.replace(archive)
    result = dict(receipt, status='qualified', archive=str(archive), archive_sha256=archive_sha,
                  archive_size=archive_size, decompressed_sha256=restored_sha,
                  qualified_at=datetime.now().isoformat(), qualified_recovery_point=True)
    _runtime_json(qualified, result)
    # Qualification is already durable. Cleanup failure cannot revoke it or
    # silently discard the remaining raw restoration input.
    cleanup_errors = []
    for path in (raw, Path(str(raw) + '.raw.json')):
        try:
            path.unlink()
        except OSError as exc:
            cleanup_errors.append(type(exc).__name__)
    return dict(result, cleanup_errors=cleanup_errors)


def retain_qualified_backups(backup_dir, keep=7, weekly_keep=4, *, deadline_epoch=None):
    """Unknown, partial or changed archives never consume a retention slot."""
    if type(keep) is not int or keep < 1 or type(weekly_keep) is not int or weekly_keep < 0:
        raise ValueError('positive daily and nonnegative weekly retention required')
    folder = Path(backup_dir).resolve()
    eligible, ignored = [], []
    try:
        for record in sorted(folder.glob('kpl_data_pre_daily_*.duckdb.gz.qualified.json'), reverse=True):
            _before_deadline(deadline_epoch)
            try:
                item = json.loads(record.read_text(encoding='utf-8'))
                archive = Path(item['archive']).resolve(strict=True)
                if archive.parent != folder or record.resolve() != Path(str(archive) + '.qualified.json'):
                    raise ValueError('qualification path escapes backup directory')
                if item.get('schema') != 1 or item.get('status') != 'qualified' or item.get('qualified_recovery_point') is not True:
                    raise ValueError('not qualified')
                with archive.open('rb') as stream:
                    sha, size = _stream_digest(stream, deadline_epoch)
                if sha != item['archive_sha256'] or size != item['archive_size']:
                    raise ValueError('qualified archive changed')
                eligible.append((record, archive))
            except TimeoutError:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                ignored.append(record.name)
    except TimeoutError:
        return {'status': 'deferred', 'deleted': [], 'reason': 'retention verification deadline exhausted'}
    kept, deleted, anchors = set(), [], 0
    for index, (_, archive) in enumerate(eligible):
        stamp = archive.name[len('kpl_data_pre_daily_'):][:8]
        try:
            monday = datetime.strptime(stamp, '%Y%m%d').weekday() == 0
        except ValueError:
            # Malformed external names are not automatically removable.
            kept.add(archive)
            continue
        if index < keep or (monday and anchors < weekly_keep):
            kept.add(archive)
            if index >= keep:
                anchors += 1
    for record, archive in eligible:
        if archive not in kept:
            _before_deadline(deadline_epoch)
            # Paths were resolved, bound to their qualification, and verified
            # within this one folder before any deletion.
            archive.unlink()
            record.unlink()
            deleted.append(archive.name)
    return {'status': 'completed', 'qualified_count': len(eligible), 'deleted': deleted,
            'kept': len(kept), 'ignored_qualification': ignored}


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


OPERATIONAL_SNAPSHOT_SCHEMA = 'operational_input_snapshot_v1'
OPERATIONAL_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024


def freeze_operational_inputs(db_path, run_dir, *, run_id, trade_date, phase,
                              evaluated_at, collector_contract_sha256, deadline_epoch,
                              observation_window_id=None):
    """Retain actual bounded inputs while the collection caller still owns its guard.

    Views are materialized with their original SQL, and reference payloads and
    canonical raw_json are retained, not just counts or readiness booleans. A
    historical receipt can never be backfilled from today's mutable tables.
    """
    import threading
    import duckdb
    from trade_system.readiness import GROUPS, _previous_open_session
    from trade_system.ths_quality import canonical_ths_snapshot
    from trade_system.time_utils import as_local_naive

    observed = as_local_naive(evaluated_at)
    now = datetime.now()
    if (phase not in {'auction', 'intraday', 'close', 'supplemental'} or observed is None
            or observed.date().isoformat() != trade_date or now.date().isoformat() != trade_date
            or not 0 <= (now - observed).total_seconds() <= 60
            or len(collector_contract_sha256) != 64
            or any(c not in '0123456789abcdef' for c in collector_contract_sha256)):
        raise ValueError('only current contract-bound operational inputs can be frozen')
    deadline = min(float(deadline_epoch), clock.time() + 15)
    if not math.isfinite(deadline) or deadline - clock.time() < 1:
        raise ValueError('operational snapshot budget exhausted')
    folder = Path(run_dir).resolve(strict=True)
    target = folder / 'operational-inputs.duckdb'
    if target.exists():
        raise ValueError('operational input snapshot already exists; never overwrite evidence')
    source = Path(db_path).resolve(strict=True)
    partial = folder / ('operational-inputs.' + uuid.uuid4().hex + '.partial.duckdb')
    binding = {'schema': OPERATIONAL_SNAPSHOT_SCHEMA, 'run_id': run_id,
               'trade_date': trade_date, 'phase': phase, 'evaluated_at': evaluated_at,
               'observation_window_id': observation_window_id,
               'collector_contract_sha256': collector_contract_sha256,
               'source_database': str(source)}
    output = duckdb.connect(str(partial))
    completed = False
    timer = threading.Timer(max(.01, deadline - clock.time()), output.interrupt)
    timer.daemon = True
    timer.start()
    try:
        output.execute("SET memory_limit='512MB'")
        output.execute("SET max_temp_directory_size='0B'")
        output.execute('SET preserve_insertion_order=false')
        output.execute('SET threads=1')
        target_catalog = output.execute('SELECT current_database()').fetchone()[0]
        output.execute("ATTACH '" + str(source).replace("'", "''") + "' AS observed (READ_ONLY)")
        # Source views may only read this already attached database. Disable
        # files, extensions and further attachments before binding their SQL.
        output.execute('SET enable_external_access=false')
        # The attached source is read-only. A transaction fixes all relation
        # reads to one snapshot even if an unrelated reader is active.
        output.execute('BEGIN TRANSACTION')
        output.execute('USE observed')
        previous = _previous_open_session(output, trade_date) if phase == 'auction' else None
        ths = canonical_ths_snapshot(output, trade_date) if phase != 'auction' else None
        output.execute('USE "' + target_catalog.replace('"', '""') + '"')
        relations = {'tushare_trade_cal'}
        if phase == 'auction':
            relations.update(name for group in ('market_state', 'candidate_pool', 'auction')
                             for name in GROUPS[group].relations)
            relations.add('realtime_candidate_pool_snapshot')
        else:
            relations.update(('tushare_stock_basic', 'multi_source_observation',
                              'intraday_stock_flow_batch', 'intraday_sector_flow_batch',
                              'intraday_sector_flow_taxonomy', 'multi_source_stock_flow',
                              'multi_source_sector_flow', 'ths_concept_daily',
                              'ths_concept_stock_history', 'ths_concept_member_checkpoint',
                              'ths_concept_snapshot_expectation'))
            if phase in {'close', 'supplemental'}:
                relations.add('v_kline_daily')
        price_view = None
        price_inputs = set()
        if 'v_kline_daily' in relations:
            view_row = output.execute("SELECT sql FROM duckdb_views() WHERE database_name='observed' "
                "AND schema_name='main' AND view_name='v_kline_daily'").fetchone()
            if view_row:
                price_view = view_row[0].split(' AS ', 1)[1]
                price_inputs = output.get_table_names(price_view, qualified=True)
                # This canonical view is partitioned by date. Copy its exact
                # dated raw sources first; expanding the global source view
                # before pushing down the date can use GB of working memory.
                if not price_inputs or not price_inputs <= {
                        'tushare_daily', 'multi_source_kline', 'kline',
                        'advanced_kline_today', 'advanced_gujia_kline'}:
                    raise ValueError('canonical price view has an unsealed input dependency')
                relations.update(price_inputs)
        inventory = []
        for name in sorted(relations):
            if clock.time() >= deadline:
                raise ValueError('operational snapshot budget exhausted')
            present = output.execute("SELECT table_type FROM information_schema.tables "
                "WHERE table_catalog='observed' AND table_schema='main' AND table_name=?", [name]).fetchone()
            if not present:
                if name in price_inputs:
                    raise ValueError('canonical price view input is missing: ' + name)
                inventory.append({'relation': name, 'present': False})
                continue
            if name == 'v_kline_daily' and price_view is not None:
                # Dependencies above are local tables only. Preserve source
                # rowids for exact tie-breaking and retain the original SQL.
                output.execute('CREATE VIEW v_kline_daily AS ' + price_view)
                count = output.execute('SELECT count(*) FROM v_kline_daily').fetchone()[0]
                if count > 300000:
                    raise ValueError('operational price snapshot row budget exceeded')
                inventory.append({'relation': name, 'present': True, 'rows': count,
                                  'source_kind': 'VIEW', 'source_view_sql': view_row[0]})
                continue
            columns = {r[0] for r in output.execute('DESCRIBE observed.main."' + name + '"').fetchall()}
            date_column = next((c for c in ('trade_date', 'date', 'source_date', 'cal_date') if c in columns), None)
            clause, params = '', []
            if name == 'multi_source_observation':
                # The stock reference validator selects its latest qualified
                # receipt (including its full original payload); the industry
                # catalogue is dated and constrained by the saved as-of clock.
                clause = " WHERE (data_type='tushare_stock_basic_snapshot' AND provider='xiaodefa' " \
                    "AND status='qualified') OR (data_type='em_industry_catalogue' AND provider='xiaodefa' " \
                    "AND source_date=CAST(? AS DATE) AND observed_at<=?) " \
                    "QUALIFY row_number() OVER (PARTITION BY data_type ORDER BY observed_at DESC)=1"
                params = [trade_date, observed]
            elif name.startswith('ths_concept_'):
                clause = ' WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)'
                params = [(ths or {}).get('snapshot_date')]
            elif date_column:
                dates = [trade_date]
                if phase == 'auction' and previous and (name == 'tushare_trade_cal'
                        or name in GROUPS['market_state'].relations):
                    dates.append(previous)
                clause = ' WHERE CAST("' + date_column + '" AS DATE) IN (' + ','.join('CAST(? AS DATE)' for _ in dates) + ')'
                params = dates
            # Materialize bounded full rows before evaluating any admission.
            # An over-limit relation is rejected, never silently truncated.
            if name in price_inputs and present[0] != 'BASE TABLE':
                raise ValueError('canonical price input must be a raw table: ' + name)
            projection = 'rowid AS rowid,*' if name in price_inputs and 'rowid' not in columns else '*'
            query = 'SELECT ' + projection + ' FROM observed.main."' + name + '"' + clause
            try:
                output.execute('CREATE TABLE "' + name + '" AS SELECT * FROM (' + query + ') LIMIT 300001', params)
            except duckdb.Error as exc:
                raise ValueError('operational snapshot relation unavailable: ' + name + ': ' + str(exc)) from exc
            count = output.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
            if count > 300000:
                raise ValueError('operational snapshot row budget exceeded: ' + name)
            definition = None
            if present[0] == 'VIEW':
                definition = output.execute("SELECT sql FROM duckdb_views() WHERE database_name='observed' "
                    "AND schema_name='main' AND view_name=?", [name]).fetchone()[0]
            inventory.append({'relation': name, 'present': True, 'rows': count,
                              'source_kind': present[0], 'source_view_sql': definition})
        binding['relations'] = inventory
        output.execute('CREATE TABLE operational_snapshot_binding(payload VARCHAR)')
        output.execute('INSERT INTO operational_snapshot_binding VALUES (?)',
                       [json.dumps(binding, sort_keys=True, ensure_ascii=False)])
        output.execute('COMMIT')
        output.execute('CHECKPOINT')
        completed = True
    finally:
        timer.cancel()
        output.close()
        if not completed:
            for unfinished in (partial, Path(str(partial) + '.wal')):
                if unfinished.exists():
                    unfinished.unlink()
    try:
        if clock.time() >= deadline or partial.stat().st_size > OPERATIONAL_SNAPSHOT_MAX_BYTES:
            raise ValueError('operational snapshot time/size budget exceeded')
        digest = hashlib.sha256(partial.read_bytes()).hexdigest()
        partial.rename(target)
        return {**binding, 'path': target.name, 'sha256': digest, 'bytes': target.stat().st_size}
    finally:
        # Only this call's unfinished artifact may be removed. Completed
        # snapshots and production database/guard files are never rewritten.
        if partial.exists():
            partial.unlink()


def verified_operational_snapshot(manifest, trade_date, collector_sha, db_path):
    """Resolve one run-local immutable snapshot and verify its complete bytes."""
    import duckdb
    saved = manifest.get('operational_input_snapshot')
    assessed = manifest.get('operational_assessment') or {}
    if not isinstance(saved, dict) or not manifest.get('_run_dir'):
        raise ValueError('operational input snapshot missing; historical windows cannot be reconstructed')
    binding = {k: saved.get(k) for k in ('schema', 'run_id', 'trade_date', 'phase', 'evaluated_at',
                                        'observation_window_id', 'collector_contract_sha256',
                                        'source_database', 'relations')}
    if (binding['schema'] != OPERATIONAL_SNAPSHOT_SCHEMA or binding['run_id'] != manifest.get('run_id')
            or binding['trade_date'] != trade_date or binding['phase'] != manifest.get('phase')
            or binding['evaluated_at'] != assessed.get('evaluated_at')
            or binding['observation_window_id'] != manifest.get('observation_window_id')
            or binding['collector_contract_sha256'] != collector_sha.lower()
            or (db_path is not None and binding['source_database'] != str(Path(db_path).resolve()))
            or saved.get('path') != 'operational-inputs.duckdb'):
        raise ValueError('operational input snapshot binding mismatch')
    folder = Path(manifest['_run_dir']).resolve(strict=True)
    candidate = folder / saved['path']
    if candidate.is_symlink() or candidate.resolve(strict=True).parent != folder:
        raise ValueError('operational input snapshot must be a run-local regular file')
    size = candidate.stat().st_size
    if not 0 < size <= OPERATIONAL_SNAPSHOT_MAX_BYTES or size != saved.get('bytes'):
        raise ValueError('operational input snapshot size mismatch')
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != saved.get('sha256'):
        raise ValueError('operational input snapshot SHA256 mismatch')
    with duckdb.connect(str(candidate), read_only=True, config={'enable_external_access': False}) as con:
        kind = con.execute("SELECT table_type FROM information_schema.tables WHERE table_schema='main' "
                           "AND table_name='operational_snapshot_binding'").fetchone()
        if kind != ('BASE TABLE',):
            raise ValueError('operational input snapshot binding must be a local table')
        rows = con.execute('SELECT payload FROM operational_snapshot_binding LIMIT 2').fetchall()
        if len(rows) != 1 or json.loads(rows[0][0]) != binding:
            raise ValueError('operational input snapshot internal binding mismatch')
    return candidate


def replay_operational_snapshot(snapshot_path, trade_date, phase, evaluated_at, *, deadline_epoch=None):
    """Recompute admission on sealed local inputs with bounded, offline SQL.

    The same restricted connection reaches every readiness query. Interrupts
    continue after the deadline because some diagnostics catch SQL exceptions.
    A swallowed interrupt can never turn an expired evaluation into admission.
    """
    import threading
    import duckdb
    from trade_system.collection_profiles import operational_readiness

    deadline = min(clock.time() + 10, float(deadline_epoch)) if deadline_epoch is not None else clock.time() + 10
    if not math.isfinite(deadline) or clock.time() >= deadline:
        raise ValueError('operational replay budget exhausted')
    with duckdb.connect(str(snapshot_path), read_only=True, config={
            'enable_external_access': False, 'memory_limit': '512MB',
            'max_temp_directory_size': '0B', 'threads': 1}) as con:
        finished = threading.Event()

        def interrupt_expired_queries():
            if finished.wait(max(0, deadline - clock.time())):
                return
            while not finished.is_set():
                con.interrupt()
                finished.wait(.01)

        watcher = threading.Thread(target=interrupt_expired_queries, daemon=True)
        watcher.start()
        try:
            result = operational_readiness(con, trade_date, phase, evaluated_at)
            if clock.time() >= deadline:
                raise ValueError('operational replay budget exhausted')
            return result
        finally:
            finished.set()
            watcher.join()


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
        self.data["completed_at"] = datetime.now().isoformat()
        if error:
            self.data["error"] = error
        if warnings:
            self.data["warnings"] = list(warnings)
        self.write()

    def write(self) -> None:
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
