from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.risk import init_trading_tables


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize operator risk and journal tables.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    tables = init_trading_tables(args.db)
    print("Initialized trading tables:", ", ".join(tables))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
