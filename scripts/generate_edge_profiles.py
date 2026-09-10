"""Build hot-money seat profiles and auction pattern outcome stats."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from trade_system.edge_profiles import (  # noqa: E402
    build_auction_pattern_stats,
    build_hot_money_profile,
)
from trade_system.logging_setup import configure  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--min-appearances", type=int, default=2)
    args = parser.parse_args()

    configure()
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    try:
        hm = build_hot_money_profile(con, min_appearances=args.min_appearances)
        ap = build_auction_pattern_stats(con)
        print(f"hot_money_profile rows: {len(hm)}")
        print(f"auction_pattern_stats rows: {len(ap)}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
