"""Collect the full-market KPL auction payload after close.

The current KPL ``/auction/market`` route returns one mapping containing all
stock-level auction sequences and final matched fields.  This replaces the
retired per-stock ``/auction/tick`` and ``/auction/bidding-anomaly`` fan-out in
the close path.  The normalized rows are idempotent and preserve the semantic
distinction between auction ticks and the final matched snapshot.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base import DuckDBStore, KPLClient
from collect_misc import collect_auction_market
from config import API_KEY, DB_PATH, TODAY
from schema import init_schema
from trade_system.api_health import require_api_key


def collect(db_path: str | Path, trade_date: str, *, out: str | Path = "") -> dict:
    target = Path(out) if out else None
    result: dict = {
        "trade_date": trade_date,
        "status": "error",
        "source": "/auction/market",
        "stock_rows": 0,
        "tick_rows": 0,
        "quote_rows": 0,
    }
    try:
        require_api_key(API_KEY)
        store = DuckDBStore(str(db_path))
        try:
            init_schema(store.conn)
            parsed = collect_auction_market(
                KPLClient(request_timeout=30, max_attempts=2), store, trade_date
            )
            result.update(parsed)
        finally:
            store.close()
    except Exception as exc:
        result.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})

    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--out", default="reports/auction_market_collection_latest.json")
    args = parser.parse_args()
    result = collect(args.db, args.date, out=args.out)
    return 0 if result.get("status") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
