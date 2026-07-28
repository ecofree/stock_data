from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.backfill import import_professional_csvs


def main() -> int:
    parser = argparse.ArgumentParser(description="Import professional trading CSV backfills.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--kline")
    parser.add_argument("--index-kline")
    parser.add_argument("--sector-capital")
    parser.add_argument("--auction-tick")
    parser.add_argument("--auction-anomaly")
    args = parser.parse_args()

    result = import_professional_csvs(
        args.db,
        {
            "kline": args.kline,
            "index_kline": args.index_kline,
            "sector_capital": args.sector_capital,
            "auction_tick": args.auction_tick,
            "auction_bidding_anomaly": args.auction_anomaly,
        },
    )
    print("Professional backfill import:")
    for table, rows in sorted(result.items()):
        print(f"{table}={rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
