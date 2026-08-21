from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.candidate_pool import build_candidate_pool


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build a deterministic QLib/flow research candidate pool.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--trade-date")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--out", default=str(root / "reports" / "qlib_candidate_pool_latest.json"))
    args = parser.parse_args()
    result = build_candidate_pool(
        args.db,
        model_id=args.model_id,
        trade_date=args.trade_date,
        limit=args.limit,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

