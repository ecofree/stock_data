from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.quality import DEFAULT_DUPLICATE_KEYS, dedupe_table
from schema import _ensure_business_indexes


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive and remove duplicate business-key rows.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    total_removed = 0
    for table, keys in DEFAULT_DUPLICATE_KEYS.items():
        result = dedupe_table(args.db, table, keys, dry_run=args.dry_run)
        total_removed += result.get("removed_rows", 0)
        print(
            f"{table}: status={result.get('status')} "
            f"duplicate_groups_before={result.get('duplicate_groups_before', 0)} "
            f"removed={result.get('removed_rows', 0)} "
            f"would_remove={result.get('would_remove_rows', 0)}"
        )
    if not args.dry_run:
        con = duckdb.connect(args.db)
        try:
            for name in (
                "uq_multi_source_stock_flow", "uq_multi_source_sector_flow",
                "uq_multi_source_kline", "uq_tushare_daily",
                "uq_tushare_daily_basic", "uq_tushare_moneyflow",
                "ix_multi_source_stock_flow", "ix_multi_source_sector_flow",
                "ix_multi_source_kline", "ix_tushare_daily",
                "ix_tushare_daily_basic", "ix_tushare_moneyflow",
            ):
                con.execute(f"DROP INDEX IF EXISTS {name}")
            _ensure_business_indexes(con)
            con.commit()
        finally:
            con.close()
    print(f"total_removed={total_removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
