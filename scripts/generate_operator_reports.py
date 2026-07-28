from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.reports.operator_report import (
    build_operator_report_snapshot,
    persist_operator_report_snapshot,
    render_operator_report_markdown,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Generate Phase 16 professional operator report.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--out", default=str(project_root / "reports" / "operator_report_latest.md"))
    args = parser.parse_args()

    snapshot = build_operator_report_snapshot(args.db, args.trade_date)
    persist_operator_report_snapshot(args.db, "daily_operator", snapshot)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_operator_report_markdown(snapshot), encoding="utf-8")
    print(f"operator_report={out}")
    print(f"candidate_count={snapshot['candidate_layer']['candidate_count']}")
    print(f"strategy_backtest_samples={snapshot['strategy_backtest']['sample_count']}")
    print(f"qlib_shadow_samples={snapshot['qlib_shadow']['sample_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
