from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.signals import generate_signals


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate trading signal tables.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--migration-root", help="Verified disposable diagnostic copy; never production")
    parser.add_argument("--trade-date", "--date", dest="trade_date")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Generate research-only signals even when same-date readiness fails.",
    )
    parser.add_argument(
        "--readiness-stage",
        choices=("premarket", "auction", "intraday", "close", "postmarket"),
        default="intraday",
    )
    args = parser.parse_args()
    result = generate_signals(
        args.db,
        args.trade_date,
        migration_root=args.migration_root,
        # Readiness is the default gate now; --allow-partial is the explicit
        # research-only escape hatch.
        require_ready=not args.allow_partial,
        readiness_stage=args.readiness_stage,
    )
    print(
        "Signals: "
        f"date={result['trade_date']} regime={result['regime']} "
        f"position={result['suggested_position_pct']}% "
        f"sectors={result['sector_count']} candidates={result['candidate_count']} "
        f"alerts={result['alert_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
