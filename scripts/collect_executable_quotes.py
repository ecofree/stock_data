"""Collect live Tencent spot quotes for candidate stocks (executable price path)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.executable_quotes import collect_executable_quotes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect candidate-only live quotes for entry-executable signals."
    )
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--auto-boost-if-kpl-stale",
        action="store_true",
        help="Double quote universe when kpl_stale_tracker is elevated.",
    )
    parser.add_argument(
        "--codes",
        default="",
        help="Optional comma-separated stock codes; default uses limit-pool/candidates.",
    )
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    result = collect_executable_quotes(
        args.db,
        args.date,
        limit=args.limit,
        codes=codes,
        auto_boost_if_kpl_stale=args.auto_boost_if_kpl_stale,
    )
    print(
        f"trade_date={result.get('trade_date')} status={result.get('status')} "
        f"requested={result.get('requested')} returned={result.get('returned', 0)} "
        f"written={result.get('written')} provider={result.get('provider')} "
        f"kpl_boost={result.get('kpl_boost')} stale={result.get('kpl_stale')}"
        + (f" error={result.get('error')}" if result.get("error") else "")
    )
    if result.get("status") == "error":
        return 2
    if result.get("status") == "empty_universe":
        return 0
    return 0 if int(result.get("written") or 0) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())