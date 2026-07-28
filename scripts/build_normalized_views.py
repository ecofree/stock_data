from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.normalize import build_normalized_views


def main() -> int:
    parser = argparse.ArgumentParser(description="Build normalized DuckDB views for trading workflows.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    views = build_normalized_views(args.db)
    print("Built normalized views:", ", ".join(views))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
