from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.reports.operator_report import (
    build_operator_report_snapshot,
    render_operator_report_markdown,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Export a read-only historical operator inventory.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--out", default=str(project_root / "reports" / "operator_report_latest.md"))
    args = parser.parse_args()

    db, out = Path(args.db).resolve(), Path(args.out).resolve()
    if out == db or (out.exists() and out.samefile(db)):
        raise ValueError("report output must not overwrite the source database")
    snapshot = build_operator_report_snapshot(db, args.trade_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_operator_report_markdown(snapshot), encoding="utf-8")
    print(f"operator_report={out}")
    print(f"candidate_count={snapshot['candidate_layer']['candidate_count']}")
    print(f"strategy_backtest_samples={snapshot['strategy_backtest']['sample_count']}")
    print(f"qlib_shadow_samples={snapshot['qlib_shadow']['sample_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
