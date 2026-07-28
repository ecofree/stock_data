"""One-time manual purge of the frozen daily_summary.raw_json blob (WP0 fallback).

The collector self-heals automatically: ``collect_market._self_heal_daily_summary_bloat``
drops and re-adds the ``raw_json`` column on the next pipeline tick.  Use this script
only to do the same thing manually/explicitly with a scalar-column backup first --
e.g. during maintenance or before the next tick.

Why DROP+ADD COLUMN
-------------------
The old nesting bug grew a single ``raw_json`` value to multi-gigabyte scale.  A value
that large cannot be allocated in memory, so ANY query that materializes it (a
``SELECT raw_json``, a ``length(raw_json)`` filter, or an UPDATE that reads the old
value) fails with "Out of Memory Error: Allocation failure".  This script therefore
never reads the blob: it backs up the scalar columns (the only decision-relevant
data) and then drops + re-adds the ``raw_json`` column -- a columnar metadata
operation that discards the blob without materializing it (validated ~instant
regardless of blob size).  All historical ``raw_json`` audit payloads are cleared;
the real breadth data lives in the scalar columns and in ``market_rise_fall``.

Usage
-----
    D:\\anaconda\\python.exe scripts\\repair_daily_summary_bloat.py --db kpl_data.duckdb --dry-run
    D:\\anaconda\\python.exe scripts\\repair_daily_summary_bloat.py --db kpl_data.duckdb --yes

MAKE AND VERIFY A SEPARATE BACKUP BEFORE RUNNING WITHOUT --dry-run.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import connect_duckdb  # noqa: E402
from config import DB_PATH  # noqa: E402

SCALAR_BACKUP_COLUMNS = (
    "date,limit_up_count,limit_down_count,rise_count,fall_count,"
    "consecutive_count,source_kind,fetched_at"
)


def _database_size(conn) -> str:
    try:
        row = conn.execute("PRAGMA database_size").fetchone()
        return str(row) if row else "unknown"
    except Exception:
        return "unknown"


def _already_cleaned(conn) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM _bloat_cleanup_done WHERE id=1").fetchone())
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Purge the frozen daily_summary.raw_json blob (safe DROP+ADD COLUMN)."
    )
    parser.add_argument("--db", default=DB_PATH, help="DuckDB database path")
    parser.add_argument(
        "--backup-dir", default="backups",
        help="Directory for the pre-purge scalar-column CSV backup",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    args = parser.parse_args()

    # Dry-run only reads, so open read-only (shares the DB with a live writer where
    # the platform allows).  The purging run needs read-write and a window with no
    # active pipeline writer (DuckDB holds an exclusive file lock while writing).
    conn = connect_duckdb(args.db, read_only=args.dry_run)
    try:
        rows = conn.execute("SELECT count(*) FROM daily_summary").fetchone()[0]
        print(f"database: {args.db}")
        print(f"database_size(before): {_database_size(conn)}")
        print(f"daily_summary rows: {rows}")
        print(f"already self-healed: {_already_cleaned(conn)}")
        if args.dry_run:
            print("DRY_RUN: no changes. Re-run without --dry-run to purge raw_json.")
            return 0

        if not args.yes:
            answer = input(
                "Drop and recreate daily_summary.raw_json "
                "(purges the blob AND all raw_json audit history)? Type 'purge': "
            ).strip().lower()
            if answer != "purge":
                print("Aborted by user; no changes written.")
                return 1

        # Back up the decision-relevant scalar columns first (does not read raw_json).
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"daily_summary_scalars_{ts}.csv"
        conn.execute(
            f"COPY (SELECT {SCALAR_BACKUP_COLUMNS} FROM daily_summary) "
            f"TO '{backup_path.as_posix()}' (HEADER, DELIMITER ',')"
        )
        print(f"scalar backup written: {backup_path}")

        # Columnar metadata op: discards the blob without materializing it.
        conn.execute("ALTER TABLE daily_summary DROP COLUMN raw_json")
        conn.execute("ALTER TABLE daily_summary ADD COLUMN raw_json VARCHAR")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _bloat_cleanup_done ("
            "id INTEGER PRIMARY KEY, done_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO _bloat_cleanup_done VALUES (1, current_timestamp) "
            "ON CONFLICT(id) DO UPDATE SET done_at=current_timestamp"
        )
        conn.execute("CHECKPOINT")
        print(f"database_size(after): {_database_size(conn)}")
        print(
            "raw_json purged. DuckDB reuses freed space rather than shrinking the "
            "file; EXPORT DATABASE + re-import to reclaim on-disk size."
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
