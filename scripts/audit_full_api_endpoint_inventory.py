from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import API_BASE, API_KEY, DB_PATH, TODAY
from scripts.audit_api_data_sources import infer_sector_code, infer_stock_code
from trade_system.api_endpoint_inventory import (
    attach_table_counts,
    build_inventory_from_project,
    install_endpoint_inventory,
    probe_inventory,
    render_inventory_report,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build and safely probe the full KPL API endpoint inventory.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--stock-code", default="")
    parser.add_argument("--sector-code", default="")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--delay", type=float, default=0.12)
    parser.add_argument("--max-endpoints", type=int, default=0)
    parser.add_argument("--no-live", action="store_true")
    parser.add_argument("--out", default=str(project_root / "reports" / "api_endpoint_inventory_latest.md"))
    parser.add_argument("--docs-out", default=str(project_root / "docs" / "integration" / "api_endpoint_inventory.md"))
    args = parser.parse_args()

    db_path = Path(args.db)
    stock_code = args.stock_code or infer_stock_code(db_path, args.date)
    sector_code = args.sector_code or infer_sector_code(db_path, args.date)

    items = build_inventory_from_project(project_root)
    attach_table_counts(items, db_path)
    if not args.no_live:
        probe_inventory(
            items,
            api_base=API_BASE,
            api_key=API_KEY,
            date=args.date,
            stock_code=stock_code,
            sector_code=sector_code,
            timeout=args.timeout,
            delay=args.delay,
            max_endpoints=args.max_endpoints or None,
        )

    inserted = install_endpoint_inventory(db_path, items)
    report = render_inventory_report(items, date=args.date, stock_code=stock_code, sector_code=sector_code)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    docs_out = Path(args.docs_out)
    docs_out.parent.mkdir(parents=True, exist_ok=True)
    docs_out.write_text(report, encoding="utf-8")

    verdict_counts = Counter(item.verdict for item in items)
    print(f"api_endpoint_inventory_rows={inserted}")
    print(f"api_endpoint_inventory_report={out}")
    print(f"api_endpoint_inventory_docs={docs_out}")
    print(f"date={args.date} stock_code={stock_code} sector_code={sector_code} live_probe={str(not args.no_live).lower()}")
    for verdict, count in sorted(verdict_counts.items()):
        print(f"{verdict}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
