from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.flow_features import build_flow_features


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build canonical multi-horizon stock and sector flow features.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--out", default=str(root / "reports" / "flow_features_latest.json"))
    args = parser.parse_args()
    result = build_flow_features(args.db, start_date=args.start_date, end_date=args.end_date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
