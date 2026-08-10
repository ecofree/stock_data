"""Generate the rich "trading war-room" dashboard (static HTML + ECharts).

A self-contained HTML page presenting the warehouse as a trading cockpit: market
emotion trend, executable candidates, capital flow, limit-up ladder, theme mainline,
index candles, auction tape, dragon-tiger and data health.  Read-only on the DB.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.terminal_report import write_terminal


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the trading war-room dashboard.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", default=None, help="Pin market-state lookups to one trade date.")
    parser.add_argument("--out", default="reports/trading_terminal_latest.html")
    args = parser.parse_args()
    out = write_terminal(args.db, args.out, args.date)
    print(f"trading terminal: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
