"""Refresh close_decision signals after TuShare/kline lag fills."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.normalize import build_normalized_views  # noqa: E402
from trade_system.stage_signals import refresh_close_signals_if_needed  # noqa: E402
from trade_system.tushare_relay import sync_tushare_ohlc_to_core_tables  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild close stage signals when same-date kline is now ready."
    )
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD")
    parser.add_argument("--run-id", default="close_refresh")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--skip-sync-ohlc",
        action="store_true",
        help="Do not push TuShare daily into the physical kline table first.",
    )
    parser.add_argument(
        "--skip-views",
        action="store_true",
        help="Do not rebuild normalized views before refresh.",
    )
    parser.add_argument("--as-of", default="", help="ISO timestamp override")
    args = parser.parse_args()

    if not args.skip_sync_ohlc:
        synced = sync_tushare_ohlc_to_core_tables(
            args.db,
            start_date=args.date,
            end_date=args.date,
        )
        print("sync_ohlc", synced)
    if not args.skip_views:
        views = build_normalized_views(args.db)
        print("views", len(views))

    result = refresh_close_signals_if_needed(
        args.db,
        args.date,
        run_id=args.run_id,
        limit=args.limit,
        freshness_seconds=None,
        strict_tradability=True,
        as_of_time=args.as_of or datetime.now(),
    )
    print(
        f"date={result.get('trade_date')} refreshed={result.get('refreshed')} "
        f"inserted={result.get('inserted', 0)} actionable={result.get('actionable', 0)} "
        f"reason={result.get('reason', result.get('close_refresh'))}"
    )
    # Non-zero only when a refresh was needed but still produced zero inserts
    # and readiness is not ready — callers can treat 0 as success either way.
    if result.get("refreshed") and int(result.get("inserted") or 0) == 0:
        return 2 if not (result.get("readiness") or {}).get("ready", True) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
