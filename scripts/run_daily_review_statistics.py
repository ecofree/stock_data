from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.review_statistics import build_daily_review_statistics, render_daily_review_statistics


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build daily review statistics for staged operator signals.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--min-return-samples", type=int, default=50)
    parser.add_argument("--out", default=str(project_root / "reports" / "daily_review_statistics_latest.md"))
    args = parser.parse_args()

    stats = build_daily_review_statistics(args.db, args.min_return_samples)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_daily_review_statistics(stats), encoding="utf-8")
    print(f"daily_review_statistics_report={out}")
    print(f"signal_samples={stats.get('sample_count', 0)}")
    for stage, item in sorted(stats.get("stage_statistics", {}).items()):
        print(f"{stage}_verdict={item.get('verdict')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
