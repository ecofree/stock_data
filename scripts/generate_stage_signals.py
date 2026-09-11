from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.stage_signals import STAGE_NAMES, generate_stage_signals


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one as-of-time trading stage signal set.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--migration-root", help="Verified disposable diagnostic copy; never production")
    parser.add_argument("--date", required=True)
    parser.add_argument("--stage", choices=STAGE_NAMES, required=True)
    parser.add_argument("--as-of", default="", help="ISO timestamp; defaults to current local time.")
    parser.add_argument("--run-id", default="manual")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--freshness-seconds", type=int, default=None)
    parser.add_argument(
        "--strict-tradability",
        action="store_true",
        help="Require positive current flow and sell-side liquidity evidence before execution-ready status.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip the signal push hook (delivery is a no-op without KPL_NOTIFY_* channels).",
    )
    parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Return zero even when signals are stored as non-actionable evidence.",
    )
    args = parser.parse_args()

    result = generate_stage_signals(
        args.db,
        args.date,
        args.stage,
        migration_root=args.migration_root,
        as_of_time=args.as_of or datetime.now(),
        run_id=args.run_id,
        limit=args.limit,
        freshness_seconds=args.freshness_seconds,
        strict_tradability=args.strict_tradability,
    )
    print(
        f"date={result['trade_date']} stage={result['stage']} source_date={result['source_trade_date']} "
        f"inserted={result['inserted']} actionable={result['actionable']} "
        f"window={str(result['within_stage_window']).lower()} "
        f"cutoff={str(result['source_cutoff_ok']).lower()}"
    )
    # Disposable diagnostics have no notification or publication authority.
    if not result.get("readiness", {}).get("ready", False):
        return 0 if args.allow_blocked else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
