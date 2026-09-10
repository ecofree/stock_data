"""Run the THS checkpoint repair explicitly under operator control."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.ths_checkpoint_migration import repair_ths_checkpoint_storage
from trade_system.pipeline_runtime import PipelineLock


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly repair THS checkpoint storage; never run from a collector."
    )
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    if args.dry_run:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            result = repair_ths_checkpoint_storage(con, dry_run=True)
        finally:
            con.close()
    else:
        run_id = f"repair_ths_checkpoint_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        with PipelineLock(db_path, run_id):
            from trade_system.db_utils import legacy_connect
            con = legacy_connect(str(db_path))
            try:
                result = repair_ths_checkpoint_storage(con)
            finally:
                con.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
