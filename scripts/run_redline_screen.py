"""CLI: Run red-line screening on today's limit-pool candidates."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.hithink_client import HiThinkClient  # noqa: E402
from trade_system.logging_setup import configure  # noqa: E402


def _to_ths(code: str) -> str:
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def main() -> int:
    parser = argparse.ArgumentParser(description="Financial red-line screening.")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--max-stocks", type=int, default=15)
    args = parser.parse_args()

    configure()
    client = HiThinkClient(min_interval=0.5)
    con = duckdb.connect(args.db, read_only=True)
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT stock_code FROM official_limit_pool WHERE trade_date=?",
        [args.trade_date]).fetchall()]
    con.close()

    from trade_system.redline_screen import screen_stock
    clean, flagged = [], []
    for i, code in enumerate(codes[:args.max_stocks]):
        ths = _to_ths(code)
        triggered = screen_stock(client, ths)
        if triggered:
            flagged.append({"code": code, "lines": triggered})
            names = ", ".join(f"{t['name']}({t['detail']})" for t in triggered)
            print(f"  ⚠️ {code}: {names}")
        else:
            clean.append(code)
            print(f"  ✓ {code}: 通过")
        if (i + 1) % 10 == 0:
            print(f"  progress: {i + 1}/{min(len(codes), args.max_stocks)}")

    print(f"\nresult: {len(clean)} clean / {len(flagged)} flagged / "
          f"{len(codes[:args.max_stocks])} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
