from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.backtest import render_backtest_markdown, run_market_regime_backtest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run market regime backtest.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--out", default="reports/backtest_latest.md")
    args = parser.parse_args()
    result = run_market_regime_backtest(args.db)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_backtest_markdown(result), encoding="utf-8")
    print(f"Backtest: samples={result['sample_count']} out={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
