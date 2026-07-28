from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.operator_views import build_operator_views


def main() -> int:
    parser = argparse.ArgumentParser(description="Build unified operator views.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    build_operator_views(args.db)
    print("Built operator views: v_operator_candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
