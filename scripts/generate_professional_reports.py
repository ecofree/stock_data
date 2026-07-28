from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.review import write_professional_reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate separated professional trading reports.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date")
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args()
    paths = write_professional_reports(args.db, args.out_dir, args.date)
    print("Professional reports:")
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
