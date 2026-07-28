from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.backfill import repair_kline_from_raw_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Expand embedded raw_json K-line payloads into structured kline rows.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    result = repair_kline_from_raw_json(args.db)
    print(
        "K-line raw_json repair: "
        f"source_rows={result['source_rows']} inserted_rows={result['inserted_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
