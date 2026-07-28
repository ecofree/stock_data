from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.quality import run_quality_audit, write_quality_report
from trade_system.schema_audit import audit_schema, load_defined_tables


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a data quality report.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--schema", default="schema.py")
    parser.add_argument("--out", default="reports/data_quality_latest.md")
    args = parser.parse_args()

    audit = run_quality_audit(args.db)
    if Path(args.schema).exists():
        audit["schema"] = audit_schema(args.db, load_defined_tables(args.schema))
    out_path = write_quality_report(audit, args.out)
    print(
        "Data quality audit: "
        f"tables={audit['summary']['table_count']} rows={audit['summary']['total_rows']} "
        f"duplicate_issues={audit['summary']['duplicate_issue_count']} out={out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
