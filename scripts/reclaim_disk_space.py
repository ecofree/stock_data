"""Reclaim on-disk space after the daily_summary blob purge (WP0 follow-up).

DuckDB does not shrink its file when data is removed; freed blocks are reused but the
footprint stays.  After the multi-gigabyte raw_json blob was purged, the file is still
~10GB even though the live data is much smaller.  This script compacts the file via the
canonical ``EXPORT DATABASE`` + ``IMPORT DATABASE`` rebuild into a fresh file, then swaps
it in.

Safety
------
* Refuses to run while a pipeline writer holds the database (advisory lock file present
  with a live PID, or an active writer connection).  Run it in a quiet window -- after
  close and before the 17:30 close run, or with the watchers stopped.
* Makes a full file-copy backup first (consistent because no writer is active and the
  WAL is checkpointed beforehand).
* Builds the compacted database in a SEPARATE file, verifies it opens and has the same
  table set, then swaps via rename.  The original is kept as ``*.pre-reclaim.old`` until
  you delete it.
* After the swap, re-runs schema init + ``ensure_operational_indexes.py`` because
  EXPORT/IMPORT does not preserve secondary indexes.
* Checks free disk space first (the export + compacted copy + backup need headroom).

Usage
-----
    D:\\anaconda\\python.exe scripts\\reclaim_disk_space.py --db kpl_data.duckdb --dry-run
    D:\\anaconda\\python.exe scripts\\reclaim_disk_space.py --db kpl_data.duckdb --yes

MAKE SURE NO PIPELINE RUN IS ACTIVE.  KEEP THE *.pre-reclaim.old FILE UNTIL YOU HAVE
VERIFIED THE COMPACTED DATABASE.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from base import connect_duckdb  # noqa: E402
from config import DB_PATH  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _database_size_bytes(conn) -> int:
    try:
        row = conn.execute(
            "SELECT total_blocks * block_size FROM pragma_database_size()").fetchone()
        return int(row[0]) if row and row[0] is not None else -1
    except Exception:
        return -1


def _human(n: int) -> str:
    if n < 0:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _writer_active(db_path: Path) -> str | None:
    """Return a reason string if a writer appears to hold the DB, else None."""
    lock = db_path.with_name(f"{db_path.name}.pipeline.lock")
    if lock.exists():
        try:
            import json
            info = json.loads(lock.read_text(encoding="utf-8"))
            pid = int(info.get("pid", 0))
            started = info.get("started_at", "?")
            # Treat the lock as live unless the PID is provably dead.
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                return f"pipeline lock held by live PID {pid} (started {started})"
        except Exception:
            return "pipeline lock file present and unreadable"
    # Probe for an active writer by attempting a read-write connect (DuckDB holds an
    # exclusive file lock while a writer is attached, so this fails if one is active).
    try:
        from trade_system.db_utils import legacy_connect
        probe = legacy_connect(str(db_path))
        probe.close()
    except duckdb.IOException as exc:
        return f"database file is locked by another process ({str(exc)[:80]})"
    except Exception as exc:
        return f"could not open database for write probe ({str(exc)[:80]})"
    return None


def _table_names(conn) -> set:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main'").fetchall()
    return {r[0] for r in rows}


def _table_schema(conn) -> list[tuple]:
    """Return a deterministic table-column signature for pre-swap checks."""
    return conn.execute(
        "SELECT table_name, column_name, ordinal_position, data_type "
        "FROM information_schema.columns WHERE table_schema='main' "
        "AND table_name IN (SELECT table_name FROM information_schema.tables WHERE table_schema='main') "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()


def _critical_row_counts(conn, relations: set[str]) -> dict[str, int]:
    """Check representative operator/research tables without scanning every table."""
    critical = {
        "kline", "index_kline", "market_state", "sector_capital",
        "stock_capital_flow", "qlib_prediction", "qlib_stock_flow_features",
        "ths_concept_daily", "ths_concept_stock_history",
        "daily_summary", "limit_up_pool",
    }
    out: dict[str, int] = {}
    for name in sorted(critical & relations):
        out[name] = int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compact kpl_data.duckdb via EXPORT+IMPORT to reclaim disk space."
    )
    parser.add_argument("--db", default=DB_PATH, help="DuckDB database path")
    parser.add_argument(
        "--backup-dir", default=None,
        help="Directory for the full-file backup (default: <db parent>/backups)")
    parser.add_argument(
        "--min-free-gb", type=float, default=25.0,
        help="Abort if free disk space is below this many GB (default 25)")
    parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return 2

    # --- Pre-flight: no active writer ---
    reason = _writer_active(db_path)
    if reason:
        print(f"REFUSING TO RUN: {reason}")
        print("Stop the pipeline / watchers and re-run in a quiet window.")
        return 3

    # --- Pre-flight: free disk space ---
    free_bytes = shutil.disk_usage(str(db_path.parent)).free
    if free_bytes < args.min_free_gb * 1e9 and not args.dry_run:
        print(
            f"REFUSING TO RUN: free space {_human(free_bytes)} < "
            f"{args.min_free_gb:.0f} GB needed for export + compacted copy + backup.")
        return 3

    # --- Report current size ---
    conn = connect_duckdb(str(db_path))
    try:
        size_before = _database_size_bytes(conn)
        tables_before = _table_names(conn)
        schema_before = _table_schema(conn)
        counts_before = _critical_row_counts(conn, tables_before)
        # Flush the WAL so the file copy backup is self-consistent.
        conn.execute("CHECKPOINT")
    finally:
        conn.close()

    print(f"database: {db_path}")
    print(f"database_size(before): {_human(size_before)}")
    print(f"tables: {len(tables_before)}")
    print(f"free disk space: {_human(free_bytes)}")

    if args.dry_run:
        print("DRY_RUN: no changes. Re-run without --dry-run to compact.")
        return 0

    if not args.yes:
        answer = input(
            "Compact this database via EXPORT+IMPORT? Type 'compact' to continue: "
        ).strip().lower()
        if answer != "compact":
            print("Aborted by user; no changes made.")
            return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = (
        Path(args.backup_dir).resolve()
        if args.backup_dir
        else db_path.parent / "backups"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{db_path.stem}.pre-reclaim.{ts}.duckdb"
    export_dir = db_path.with_name(f"{db_path.name}.export.{ts}")
    compacted_path = db_path.with_name(f"{db_path.name}.compacted.{ts}")
    old_path = db_path.with_name(f"{db_path.name}.pre-reclaim.old")

    try:
        # --- 1. Full-file backup (DB is quiescent + checkpointed) ---
        print(f"[1/6] backing up to {backup_path} ...")
        shutil.copy2(str(db_path), str(backup_path))

        # --- 2. EXPORT DATABASE to a fresh directory ---
        print(f"[2/6] exporting database to {export_dir} ...")
        if export_dir.exists():
            shutil.rmtree(str(export_dir), ignore_errors=True)
        econn = connect_duckdb(str(db_path))
        try:
            econn.execute(f"EXPORT DATABASE '{export_dir.as_posix()}' (FORMAT PARQUET)")
        finally:
            econn.close()

        # --- 3. IMPORT into a fresh compacted file ---
        print(f"[3/6] importing into compacted file {compacted_path} ...")
        if compacted_path.exists():
            compacted_path.unlink()
        cconn = connect_duckdb(str(compacted_path))
        try:
            cconn.execute(f"IMPORT DATABASE '{export_dir.as_posix()}'")
            cconn.execute("CHECKPOINT")
            tables_after = _table_names(cconn)
            schema_after = _table_schema(cconn)
            counts_after = _critical_row_counts(cconn, tables_after)
            size_after = _database_size_bytes(cconn)
        finally:
            cconn.close()

        # --- 4. Verify the compacted database ---
        if tables_before != tables_after:
            missing = sorted(tables_before - tables_after)
            extra = sorted(tables_after - tables_before)
            print(
                "REFUSING TO SWAP: compacted DB relation set differs; "
                f"missing={missing[:20]} extra={extra[:20]}"
            )
            print(f"Compacted file left at {compacted_path}; backup at {backup_path}.")
            return 4
        if schema_before != schema_after:
            print("REFUSING TO SWAP: compacted table schema differs from source.")
            print(f"Compacted file left at {compacted_path}; backup at {backup_path}.")
            return 4
        if counts_before != counts_after:
            print(
                "REFUSING TO SWAP: critical table row counts changed; "
                f"before={counts_before} after={counts_after}"
            )
            print(f"Compacted file left at {compacted_path}; backup at {backup_path}.")
            return 4
        print(f"[4/6] verified: {len(tables_after)} tables, "
              f"compacted size {_human(size_after)}")

        # A watcher may start between the initial pre-flight and this point.
        # Never swap a live database after the lock has been reacquired.
        reason = _writer_active(db_path)
        if reason:
            print(f"REFUSING TO SWAP: {reason}")
            print(f"Compacted file left at {compacted_path}; backup at {backup_path}.")
            return 4

        # --- 5. Swap (keep original as *.pre-reclaim.old) ---
        print("[5/6] swapping files ...")
        if old_path.exists():
            old_path.unlink()
        os.replace(str(db_path), str(old_path))
        os.replace(str(compacted_path), str(db_path))
        # Drop a stale WAL belonging to the old file, if any.
        stale_wal = db_path.with_name(f"{db_path.name}.wal")
        # The compacted file was checkpointed, so no WAL should accompany it; if a
        # leftover .wal from the old DB exists, remove it so it is not replayed.
        if stale_wal.exists():
            try:
                stale_wal.unlink()
            except Exception:
                pass

        # --- 6. Restore schema guarantees + secondary indexes ---
        print("[6/6] re-applying schema + operational indexes ...")
        from schema import init_schema
        iconn = connect_duckdb(str(db_path))
        try:
            init_schema(iconn)
            iconn.execute("CHECKPOINT")
        finally:
            iconn.close()
        idx_script = ROOT / "scripts" / "ensure_operational_indexes.py"
        if idx_script.exists():
            subprocess.run(
                [sys.executable, str(idx_script), "--db", str(db_path)],
                cwd=str(ROOT), check=True)
        # Rebuild the normalized signal/report views (belt-and-suspenders in case
        # EXPORT/IMPORT did not carry every view definition).
        views_script = ROOT / "scripts" / "build_normalized_views.py"
        if views_script.exists():
            subprocess.run(
                [sys.executable, str(views_script), "--db", str(db_path)],
                cwd=str(ROOT), check=True)

        # Strict post-swap validation.  If the rebuilt file cannot satisfy the
        # same core contract, restore the original and leave the candidate for
        # forensic inspection instead of reporting a false success.
        try:
            verify = duckdb.connect(str(db_path), read_only=True)
            try:
                final_tables = _table_names(verify)
                final_counts = _critical_row_counts(verify, final_tables)
                final_views = {
                    row[0] for row in verify.execute(
                        "SELECT table_name FROM information_schema.views WHERE table_schema='main'"
                    ).fetchall()
                }
                required_views = {
                    "v_kline_daily", "v_default_concept_daily",
                    "v_default_concept_stock_history",
                }
                if (
                    final_tables != tables_before
                    or final_counts != counts_before
                    or not required_views <= final_views
                ):
                    raise RuntimeError(
                        "post-swap contract mismatch: "
                        f"tables={final_tables == tables_before}, "
                        f"counts={final_counts == counts_before}, "
                        f"missing_views={sorted(required_views - final_views)}"
                    )
                verify.execute("SELECT 1").fetchone()
            finally:
                verify.close()
        except Exception:
            broken_path = db_path.with_name(f"{db_path.name}.post-reclaim-invalid.{ts}")
            if db_path.exists() and old_path.exists():
                os.replace(str(db_path), str(broken_path))
                os.replace(str(old_path), str(db_path))
            raise

        print(f"database_size(after): {_human(size_after)}")
        if size_before > 0 and size_after > 0:
            print(f"reclaimed: {_human(size_before - size_after)}")
        print(f"Original kept at {old_path} -- delete it once you have verified the DB.")
        print(f"Backup at {backup_path}. Export dir {export_dir} can be removed.")
        return 0
    except Exception as exc:
        print(f"ERROR during reclaim: {exc}")
        print(f"Backup (if created): {backup_path}")
        print(f"Compacted file (if created): {compacted_path}")
        print("The original database was NOT modified unless the swap step completed.")
        return 5
    finally:
        # Best-effort cleanup of the export dir on success is left to the operator so
        # they can inspect it; nothing is auto-deleted.
        _ = time.time()


if __name__ == "__main__":
    raise SystemExit(main())
