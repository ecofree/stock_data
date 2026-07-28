from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.legacy_a_share import import_legacy_tables


def render_markdown(result: dict[str, int]) -> str:
    lines = ["# Legacy A-share Import", "", "| Table | Rows |", "|---|---:|"]
    for name, count in sorted(result.items()):
        lines.append(f"| {name} | {count} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import legacy A-share kpl-qds tables into stock_data DuckDB.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--legacy-root", default=r"D:\accio\A-share\kpl-qds")
    parser.add_argument("--out", default="reports/legacy_import_latest.md")
    args = parser.parse_args()

    result = import_legacy_tables(args.db, args.legacy_root)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(result), encoding="utf-8")
    print(f"legacy_import={out}")
    for name, count in sorted(result.items()):
        print(f"{name}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
