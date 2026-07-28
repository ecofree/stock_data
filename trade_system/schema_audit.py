"""Schema audit helpers for DuckDB-backed KPL data."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import duckdb


CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def parse_defined_tables(schema_text: str) -> list[str]:
    """Return table names defined by CREATE TABLE IF NOT EXISTS statements."""
    return CREATE_TABLE_RE.findall(schema_text)


def load_defined_tables(schema_path: str | Path) -> list[str]:
    return parse_defined_tables(Path(schema_path).read_text(encoding="utf-8"))


def list_actual_tables(db_path: str | Path) -> list[str]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
    finally:
        con.close()


def audit_schema(db_path: str | Path, defined_tables: Iterable[str]) -> dict:
    defined = list(dict.fromkeys(defined_tables))
    actual = list_actual_tables(db_path)
    defined_set = set(defined)
    actual_set = set(actual)
    return {
        "defined_count": len(defined),
        "actual_count": len(actual),
        "missing_defined": [name for name in defined if name not in actual_set],
        "extra_actual": [name for name in actual if name not in defined_set],
    }


def render_schema_audit_markdown(result: dict) -> str:
    lines = [
        "# Schema Audit",
        "",
        f"- Defined tables: {result['defined_count']}",
        f"- Actual tables: {result['actual_count']}",
        f"- Missing defined tables: {len(result['missing_defined'])}",
        f"- Extra actual tables: {len(result['extra_actual'])}",
        "",
        "## Missing Defined Tables",
    ]
    lines.extend(f"- `{name}`" for name in result["missing_defined"][:200])
    lines.extend(["", "## Extra Actual Tables"])
    lines.extend(f"- `{name}`" for name in result["extra_actual"][:200])
    lines.append("")
    return "\n".join(lines)
