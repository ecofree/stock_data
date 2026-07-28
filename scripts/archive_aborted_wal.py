"""Archive abandoned DuckDB WAL remnants without touching the live WAL.

The collector deliberately renames a WAL after an interrupted run.  Keeping
all of those files beside the active database consumes space and makes health
audits noisy.  This command is dry-run by default; ``--apply`` writes and
verifies a ZIP archive before deleting only the matching ``.wal.aborted-*``
files.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import zipfile


def archive_aborted_wal(db_path: str | Path, archive: str | Path | None = None, *, apply: bool = False) -> dict:
    db = Path(db_path).resolve()
    parent = db.parent
    files = sorted(
        item.resolve()
        for item in parent.glob(f"{db.name}.wal.aborted-*")
        if item.is_file()
    )
    if any(item.parent != parent for item in files):
        raise ValueError("refusing WAL paths outside the database directory")
    total = sum(item.stat().st_size for item in files)
    result = {
        "status": "dry_run" if not apply else "empty",
        "files": len(files),
        "bytes": total,
        "archive": str(Path(archive).resolve()) if archive else "",
    }
    if not files or not apply:
        return result
    target = Path(archive).resolve() if archive else parent / "backups" / f"aborted_wal_{datetime.now():%Y%m%d_%H%M%S}.zip"
    if target.parent != parent and parent not in target.parents:
        raise ValueError("archive must stay under the database workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in files:
            bundle.write(item, arcname=item.name)
    with zipfile.ZipFile(target, "r") as bundle:
        names = set(bundle.namelist())
        if names != {item.name for item in files}:
            raise RuntimeError("WAL archive verification failed: member list mismatch")
        archived_bytes = sum(bundle.getinfo(item.name).file_size for item in files)
    if archived_bytes != total:
        raise RuntimeError("WAL archive verification failed: byte count mismatch")
    for item in files:
        item.unlink()
    result.update({"status": "archived", "archive": str(target), "remaining": len(list(parent.glob(f"{db.name}.wal.aborted-*")))})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive abandoned DuckDB WAL files safely.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--archive", default="")
    parser.add_argument("--apply", action="store_true", help="Create and verify an archive, then remove archived remnants.")
    args = parser.parse_args()
    result = archive_aborted_wal(args.db, args.archive or None, apply=args.apply)
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
