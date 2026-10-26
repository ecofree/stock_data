from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.operator_outcomes import import_operator_trade_outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description="RETIRED: legacy operator outcome import is disabled.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--csv", required=True, help="CSV with trade_date, stock_code, execution_status and review columns.")
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    args = parser.parse_args()

    try:
        import_operator_trade_outcomes(
            args.db,
            args.csv,
            fee_rate=args.fee_rate,
            slippage_bps=args.slippage_bps,
        )
    except ValueError as exc:
        print(f"operator outcome import blocked: {exc}", file=sys.stderr)
        return 2
    raise RuntimeError("retired import unexpectedly returned")


if __name__ == "__main__":
    raise SystemExit(main())
