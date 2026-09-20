"""Bounded auction acquisition with distinct market, tick and anomaly products.

The close task uses the market sequence and final-match payload. Explicit
maintenance may request scoped ticks or global anomalies; neither product is
inferred from a final-match snapshot. All share the existing store and budget.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.data_store import DuckDBStore, KPLClient
from collectors.collect_misc import collect_auction_market, collect_auction_tick, collect_auction_bidding_anomaly
from trade_system.config import API_KEY, DB_PATH, TODAY
from trade_system.schema import init_schema
from trade_system.api_health import require_api_key


def collect(db_path: str | Path, trade_date: str, *, out: str | Path = "",
            product: str = 'market', codes=(), budget_seconds: float = 60) -> dict:
    # Distinct endpoints remain distinct products; no legacy signal-based fan-out.
    if product not in {'market', 'tick', 'anomaly'} or (product == 'tick' and not codes):
        raise ValueError('supported auction product and explicit codes for tick required')
    target = Path(out) if out else None
    result: dict = {
        "trade_date": trade_date,
        "status": "error",
        "source": {'market': '/auction/market', 'tick': '/auction/tick',
                   'anomaly': '/auction/bidding-anomaly'}[product],
        "product": product,
        "stock_rows": 0,
        "tick_rows": 0,
        "quote_rows": 0,
    }
    try:
        require_api_key(API_KEY)
        store = DuckDBStore(str(db_path))
        try:
            init_schema(store.conn)
            client = KPLClient(request_timeout=30, max_attempts=2, total_budget_seconds=budget_seconds)
            if product == 'market':
                parsed = collect_auction_market(client, store, trade_date)
            else:
                collector = collect_auction_tick if product == 'tick' else collect_auction_bidding_anomaly
                rows = collector(client, store, trade_date, list(dict.fromkeys(codes)) or [''])
                failures = sum(int(client.stats.get(k) or 0) for k in ('error', 'rate_limited', 'circuit_open'))
                parsed = {product + '_rows': rows,
                          'status': 'partial' if failures and rows else 'success' if rows else 'error'}
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
    parser.add_argument('--product', choices=('market', 'tick', 'anomaly'), default='market',
                        help='Distinct market sequence/final snapshot, scoped ticks or global anomalies; never interchangeable.')
    parser.add_argument('--codes', default='', help='Explicit comma-separated codes for the tick product.')
    parser.add_argument('--budget-seconds', type=float, default=60)
    parser.add_argument("--out", default="reports/auction_market_collection_latest.json")
    args = parser.parse_args()
    result = collect(args.db, args.date, out=args.out, product=args.product,
                     codes=[c.strip() for c in args.codes.split(',') if c.strip()], budget_seconds=args.budget_seconds)
    return 0 if result.get("status") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
