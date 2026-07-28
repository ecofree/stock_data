from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.operator_backtest import render_operator_backtest_markdown, run_operator_stage_backtest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run professional operator staged backtest.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--out", default="reports/operator_backtest_latest.md")
    args = parser.parse_args()

    result = run_operator_stage_backtest(args.db, enforce_t1=True)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_operator_backtest_markdown(result), encoding="utf-8")
    print(f"operator_backtest_samples={result['sample_count']}")
    print(f"out={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
