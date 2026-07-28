from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.daily_review import build_daily_review_context, write_daily_review


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Generate daily operator review report.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--out", default=str(project_root / "reports" / "daily_review_latest.md"))
    args = parser.parse_args()

    context = build_daily_review_context(args.db, args.trade_date or None)
    path = write_daily_review(args.db, args.out, context["trade_date"])
    print(f"daily_review_report={path}")
    print(f"trade_date={context['trade_date']}")
    print(f"plans={len(context.get('plans', []))}")
    print(f"journal={len(context.get('journal', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
