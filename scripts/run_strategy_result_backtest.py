from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.strategy.schema import persist_strategy_backtest_summary
from trade_system.strategy_stage_backtest import (
    render_strategy_backtest_markdown,
    run_strategy_result_backtest,
    summarize_strategy_backtest,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Backtest tickflow-style strategy scan results.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "strategy_backtest_latest.md"))
    args = parser.parse_args()

    result = run_strategy_result_backtest(args.db, enforce_t1=True)
    summary_rows = summarize_strategy_backtest(result)
    persisted = persist_strategy_backtest_summary(args.db, summary_rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_strategy_backtest_markdown(result), encoding="utf-8")
    print(f"strategy_backtest_report={out}")
    print(f"strategy_backtest_samples={result['sample_count']}")
    print(f"strategy_backtest_summary_rows={persisted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
