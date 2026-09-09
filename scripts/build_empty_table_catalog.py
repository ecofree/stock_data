from __future__ import annotations

import argparse
from pathlib import Path
from datetime import datetime
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.empty_table_catalog import build_empty_table_catalog, render_empty_table_catalog


def _latest_official_audit(project_root: Path) -> str:
    candidates = sorted(
        (path for path in (project_root / "reports").glob("kpl_official_api_audit_*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return ""
    newest = candidates[0]
    age_hours = (datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)).total_seconds() / 3600
    return str(newest) if 0 <= age_hours <= 72 else ""


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Classify empty tables by endpoint/data-source status.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "empty_table_catalog_latest.md"))
    parser.add_argument(
        "--official-audit",
        default="",
        help="KPL route audit JSON; omitted means use the newest file not older than 72 hours.",
    )
    args = parser.parse_args()

    official_audit = args.official_audit or _latest_official_audit(project_root)
    catalog = build_empty_table_catalog(args.db, official_audit_path=official_audit or None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_empty_table_catalog(catalog), encoding="utf-8")
    print(f"empty_table_catalog_report={out}")
    for name, count in sorted(catalog["summary"].items()):
        print(f"{name}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
