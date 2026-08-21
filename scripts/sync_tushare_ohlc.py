"""Sync TuShare daily OHLC into core ``kline`` / ``index_kline`` tables."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.tushare_relay import sync_tushare_ohlc_to_core_tables  # noqa: E402
from trade_system.tushare_history import TushareHistoryCollector  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync TuShare OHLC into kline tables.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument(
        "--repair-close-gaps",
        action="store_true",
        help="Repair up to three recent daily/basic/adj_factor gaps before syncing core kline tables.",
    )
    parser.add_argument("--repair-budget-seconds", type=float, default=180.0)
    args = parser.parse_args()
    if args.repair_close_gaps:
        with TushareHistoryCollector(args.db, budget_seconds=args.repair_budget_seconds) as collector:
            repair = collector.run(
                args.start_date,
                args.end_date,
                datasets=("daily", "daily_basic", "adj_factor"),
                max_days=3,
                gap_only=True,
                retry_passes=2,
                retry_delay_seconds=5.0,
            )
        summary = {}
        for item in repair["results"]:
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        repair_text = " ".join(f"{key}={value}" for key, value in sorted(summary.items())) or "status=up_to_date"
        print("repair_tushare_close_gaps " + repair_text)
        if summary.get("error", 0) or summary.get("budget_exhausted", 0):
            return 2
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
