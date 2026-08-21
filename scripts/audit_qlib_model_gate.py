from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.ml.model_gate import audit_model_promotion


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Audit QLib model promotion gates without changing signal impact.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--model-id")
    parser.add_argument("--promote", action="store_true", help="Promote exactly one eligible model; otherwise audit only.")
    parser.add_argument("--out", default=str(root / "reports" / "qlib_model_gate_latest.json"))
    args = parser.parse_args()
    result = audit_model_promotion(args.db, model_id=args.model_id, promote=args.promote)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"qlib_model_gate={out} models={len(result['models'])} champion={result.get('champion')}")
    for item in result["models"]:
        print(f"model={item['model_id']} eligible={item['eligible']} reasons={';'.join(item['reasons'][:3])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

