from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.empty_table_catalog import build_empty_table_catalog, render_empty_table_catalog


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Classify empty tables by endpoint/data-source status.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "empty_table_catalog_latest.md"))
    args = parser.parse_args()

    catalog = build_empty_table_catalog(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_empty_table_catalog(catalog), encoding="utf-8")
    print(f"empty_table_catalog_report={out}")
    for name, count in sorted(catalog["summary"].items()):
        print(f"{name}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
