from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.auction_evidence import (
    build_auction_evidence_status,
    persist_auction_evidence_snapshot,
    render_auction_evidence_report,
    resolve_auction_trade_date,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build stock-level auction evidence chain with honest fallbacks.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default="", help="Defaults to latest available auction evidence date.")
    parser.add_argument("--out", default=str(project_root / "reports" / "auction_evidence_latest.md"))
    args = parser.parse_args()

    trade_date = resolve_auction_trade_date(args.db, args.trade_date or None)
    status = build_auction_evidence_status(args.db, trade_date)
    inserted = persist_auction_evidence_snapshot(args.db, status["rows"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_auction_evidence_report(status), encoding="utf-8")
    print(f"auction_evidence_report={out}")
    print(f"auction_evidence_rows={inserted}")
    for name, count in status["counts"].items():
        print(f"{name}={count}")
    print(f"gap_count={len(status['gaps'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
