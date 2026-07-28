from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.backfill import derive_index_kline_from_daily_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive fallback index_kline rows from real /daily raw_json fields.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    result = derive_index_kline_from_daily_summary(args.db)
    print(
        "Index K-line derive: "
        f"source_rows={result['source_rows']} inserted_rows={result['inserted_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
