from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH
from trade_system.multi_source_store import MultiSourceStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed migrated tables from existing curated core tables.")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()
    with MultiSourceStore(args.db) as store:
        counts = store.bootstrap_from_core()
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

