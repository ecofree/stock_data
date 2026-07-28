from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH, TODAY
from trade_system.capital_flow_health import (
    assess_capital_flow_health,
    render_capital_flow_health_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check stored stock and sector capital-flow freshness.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--expected-stocks", type=int, default=0)
    parser.add_argument("--expected-sectors", type=int, default=0)
    parser.add_argument("--out", default="reports/capital_flow_freshness_latest.md")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--min-coverage-pct", type=float, default=99.5)
    parser.add_argument("--collected-after", default=None)
    parser.add_argument("--max-age-seconds", type=int, default=None)
    args = parser.parse_args()

    result = assess_capital_flow_health(
        args.db,
        args.date,
        args.expected_stocks,
        args.expected_sectors,
        collected_after=args.collected_after,
        min_coverage_pct=args.min_coverage_pct,
        max_age_seconds=args.max_age_seconds,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_capital_flow_health_markdown(result), encoding="utf-8")
    print(
        f"date={args.date} ready={str(result['ready']).lower()} "
        f"stock_ready={str(result['stock_flow']['ready']).lower()} "
        f"sector_ready={str(result['sector_flow']['ready']).lower()} out={out}"
    )
    return 0 if result["ready"] or args.report_only else 2


if __name__ == "__main__":
    raise SystemExit(main())
