"""Archive operational-legacy tables out of the production DuckDB.

Targets three clearly-safe families (see docs/table_naming.md):
  - ``legacy_qds_%``        whole-table imports of the retired qds project
  - ``_dedupe_archive_%``   snapshots taken while repairing duplicate rows
  - ``_corrupt_%``          known-corrupt batches kept for forensics

Default is a dry-run report only.  ``--execute`` copies each table into
``archive_legacy.duckdb`` (ATTACHed), verifies row counts, then drops it
from the main database — one transaction per table.  The script refuses to
run while the pipeline lock exists.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

TARGET_PATTERNS = ("legacy_qds\\_%", "\\_dedupe\\_archive\\_%", "\\_corrupt\\_%")
DEFAULT_ARCHIVE_NAME = "archive_legacy.duckdb"


def find_tables(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int]]:
    where = " OR ".join(f"table_name LIKE '{p}' ESCAPE '\\'" for p in TARGET_PATTERNS)
    rows = con.execute(
        f"SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema='main' AND table_type='BASE TABLE' AND ({where}) "
        f"ORDER BY table_name"
    ).fetchall()
    out = []
    for (name,) in rows:
        count = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        out.append((name, int(count)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument(
        "--archive-db",
        default=None,
        help="Archive database path (default: <db dir>/archive_legacy.duckdb).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually move tables. Without this flag only a report is written.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}")
        return 1
    lock = db_path.parent / (db_path.name + ".pipeline.lock")
    if lock.exists():
        print(f"refusing to run while pipeline lock exists: {lock}")
        return 1
    archive_path = Path(args.archive_db) if args.archive_db else db_path.parent / DEFAULT_ARCHIVE_NAME

    con = duckdb.connect(str(db_path))
    try:
        tables = find_tables(con)
        total_rows = sum(count for _, count in tables)
        print(f"found {len(tables)} legacy table(s), {total_rows} row(s) total")
        for name, count in tables:
            print(f"  {name}: {count} rows")

        if not args.execute:
            print("dry-run only; pass --execute to move these tables")
            return 0
        if not tables:
            return 0

        con.execute("ATTACH ? AS archive (READ_WRITE)", [str(archive_path)])
        moved = 0
        for name, count in tables:
            con.execute("BEGIN TRANSACTION")
            try:
                con.execute(f'CREATE TABLE IF NOT EXISTS archive."{name}" AS SELECT * FROM main."{name}" WHERE false')
                con.execute(f'DELETE FROM archive."{name}"')
                con.execute(f'INSERT INTO archive."{name}" SELECT * FROM main."{name}"')
                archived = con.execute(f'SELECT count(*) FROM archive."{name}"').fetchone()[0]
                if int(archived) != count:
                    raise RuntimeError(f"row mismatch for {name}: {archived} != {count}")
                con.execute(f'DROP TABLE main."{name}"')
                con.execute("COMMIT")
                moved += 1
                print(f"moved {name} ({count} rows)")
            except Exception as exc:
                con.execute("ROLLBACK")
                print(f"FAILED {name}: {exc} — stopping, remaining tables untouched")
                break
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report = PROJECT_ROOT / "reports" / "legacy_table_archive_latest.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            "# Legacy table archive\n\n"
            f"- run at: {stamp}\n"
            f"- archive db: `{archive_path}`\n"
            f"- moved: {moved}/{len(tables)} table(s), "
            f"{sum(c for n, c in tables[:moved])} row(s)\n",
            encoding="utf-8",
        )
        print(f"report: {report}")
        return 0 if moved == len(tables) else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
