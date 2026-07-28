from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integrity import repair_critical_integrity
from trade_system.normalize import build_normalized_views


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair critical business-key duplicates and indexes.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = repair_critical_integrity(args.db, dry_run=args.dry_run)
    if not args.dry_run:
        build_normalized_views(args.db)
    compact = {
        "dry_run": result["dry_run"],
        "normalized_ktype_rows": result["normalized_ktype_rows"],
        "removed_rows": {
            item["table"]: item.get("would_remove_rows", 0)
            for item in result["repairs"]
            if item.get("would_remove_rows", 0)
        },
        "plan_rows_blocked": result["plan_rows_blocked"],
        "unique_indexes": result["unique_indexes"],
    }
    print(json.dumps(compact, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
