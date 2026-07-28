from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH, TODAY
from trade_system.multi_source_audit import audit_multisource, render_multisource_readiness


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit of migrated multi-source freshness.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--as-of", default=TODAY)
    parser.add_argument("--out", default="reports/multisource_readiness_latest.md")
    args = parser.parse_args()
    result = audit_multisource(args.db, args.as_of)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_multisource_readiness(result), encoding="utf-8")
    print(f"db={args.db} out={out} stock_flow={result['capital_flow'].get('multi_source_stock_flow', {}).get('status')} sector_flow={result['capital_flow'].get('multi_source_sector_flow', {}).get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

