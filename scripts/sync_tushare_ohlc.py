"""Sync TuShare daily OHLC into core ``kline`` / ``index_kline`` tables."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.tushare_relay import sync_tushare_ohlc_to_core_tables  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync TuShare OHLC into kline tables.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD or YYYYMMDD")
    args = parser.parse_args()
    result = sync_tushare_ohlc_to_core_tables(
        args.db,
        start_date=args.start_date or None,
        end_date=args.end_date or None,
    )
    print(
        "sync_tushare_ohlc "
        + " ".join(f"{key}={value}" for key, value in sorted(result.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
