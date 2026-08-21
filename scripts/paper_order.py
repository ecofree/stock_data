from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.paper_execution import simulate_fill, submit_paper_order


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Create or fill a paper-only A-share order.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stock-code", required=True)
    parser.add_argument("--side", choices=["buy", "sell"], required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--limit-price", type=float, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--approve", action="store_true", help="Allow paper pending state; still never contacts a broker.")
    parser.add_argument("--fill-price", type=float)
    parser.add_argument("--fill-quantity", type=int)
    args = parser.parse_args()
    result = submit_paper_order(
        args.db,
        order_id=args.order_id,
        idempotency_key=args.idempotency_key,
        trade_date=args.trade_date,
        stock_code=args.stock_code,
        side=args.side,
        quantity=args.quantity,
        limit_price=args.limit_price,
        candidate_model_id=args.model_id,
        risk_approved=args.approve,
        tradable=args.approve,
    )
    if result.get("status") == "pending" and args.fill_price is not None:
        result = simulate_fill(
            args.db,
            order_id=args.order_id,
            fill_price=args.fill_price,
            fill_quantity=args.fill_quantity,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

