"""Retired legacy compaction entry point; read-only inspection remains available.

Replacing a database is an approved maintenance operation, not a scheduled
collection step. Use the V2 backup/recovery workflow after explicit approval.
Neither --yes nor --dry-run grants database write authority.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb


def inspect_database(path):
    path = Path(path).resolve(strict=True)
    with duckdb.connect(str(path), read_only=True) as con:
        tables = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_type='BASE TABLE'"
        ).fetchone()[0]
    return {'database': str(path), 'bytes': path.stat().st_size, 'tables': tables,
            'source_modified': False, 'compaction_authorized': False,
            'status': 'read_only_inventory_not_restore_verification'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='kpl_data.duckdb')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args()
    if not args.dry_run or args.yes:
        parser.error('legacy automatic compaction retired; approved maintenance/recovery required')
    print(json.dumps(inspect_database(args.db), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
