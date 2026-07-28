from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.legacy_a_share import audit_legacy_project


def render_markdown(result: dict) -> str:
    lines = [
        "# A-share Legacy Project Audit",
        "",
        f"- Legacy root: `{result['legacy_root']}`",
        f"- Database exists: `{result['database']['exists']}`",
        f"- Database size: `{result['database']['size']}` bytes",
        "",
        "## Tables",
        "",
        "| Table | Rows | Min Date | Max Date |",
        "|---|---:|---|---|",
    ]
    for name, item in sorted(result.get("tables", {}).items()):
        lines.append(f"| {name} | {item['row_count']} | {item['min_date'] or ''} | {item['max_date'] or ''} |")
    lines.extend(
        [
            "",
            "## Import Tables",
            "",
            ", ".join(result["import_tables"]),
            "",
            "## Port Files",
            "",
            ", ".join(result["port_files"]),
            "",
            "## Discard Files",
            "",
            ", ".join(result["discard_files"]),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit legacy A-share kpl-qds project before consolidation.")
    parser.add_argument("--legacy-root", default=r"D:\accio\A-share\kpl-qds")
    parser.add_argument("--out", default="reports/integration_audit_latest.md")
    args = parser.parse_args()

    result = audit_legacy_project(args.legacy_root)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(result), encoding="utf-8")
    print(f"integration_audit={out}")
    print(f"database_exists={result['database']['exists']}")
    print(f"table_count={len(result.get('tables', {}))}")
    return 0 if result["database"]["exists"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
