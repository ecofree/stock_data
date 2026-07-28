from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.schema_audit import audit_schema, load_defined_tables, render_schema_audit_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit schema.py definitions against a DuckDB file.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--schema", default="schema.py")
    parser.add_argument("--out")
    args = parser.parse_args()

    result = audit_schema(args.db, load_defined_tables(args.schema))
    markdown = render_schema_audit_markdown(result)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
    print(
        "Schema audit: "
        f"defined={result['defined_count']} actual={result['actual_count']} "
        f"missing={len(result['missing_defined'])} extra={len(result['extra_actual'])}"
    )
    if result["missing_defined"]:
        print("Missing sample:", ", ".join(result["missing_defined"][:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
