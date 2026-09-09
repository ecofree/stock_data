"""Build a read-only maintenance map for scripts, relations, and pipeline tasks.

The report is deliberately evidence-first.  It does not delete tables, alter
the database, move files, or infer that an empty relation is safe to retire.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import sys
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.maintenance_registry import script_lifecycle


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _source_files(root: Path) -> list[Path]:
    skipped = {".git", ".venv", ".venv-qlib", "__pycache__", ".pytest_cache", "mlruns"}
    return [
        path
        for path in root.rglob("*.py")
        if not any(part in skipped for part in path.parts)
    ]


def collect_script_catalog(root: Path) -> dict[str, Any]:
    runner_source = (root / "scripts" / "run_integrated_daily.py").read_text(encoding="utf-8-sig")
    runner_tree = ast.parse(runner_source, filename="run_integrated_daily.py")
    runner_commands = {
        Path(str(node.value).replace("\\", "/")).name
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and str(node.value).replace("\\", "/").endswith(".py")
        and "/" in str(node.value).replace("\\", "/")
    }
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("scripts/*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        name = path.name
        if name == "run_integrated_daily.py":
            category = "canonical_pipeline"
        elif name.startswith("audit_"):
            category = "audit"
        elif name.startswith("backfill_"):
            category = "backfill"
        elif name.startswith("collect_"):
            category = "collector"
        elif name.startswith("generate_"):
            category = "report"
        elif name.startswith("run_"):
            category = "runner_or_research"
        elif name.startswith("check_"):
            category = "check"
        elif name.startswith("build_"):
            category = "build"
        elif name.startswith("sync_"):
            category = "sync"
        else:
            category = "other"
        rows.append(
            {
                "name": name,
                "category": category,
                "imports_trade_system": "trade_system" in text,
                "mentioned_by_integrated_runner": name in runner_commands,
                "runner_command_literal": name in runner_commands,
                "lifecycle": script_lifecycle(name, mentioned_by_runner=name in runner_commands),
                "lines": len(text.splitlines()),
            }
        )
    counts = Counter(row["category"] for row in rows)
    return {"rows": rows, "counts": counts}


def collect_relation_catalog(db_path: Path, source_files: list[Path]) -> dict[str, Any]:
    schema_files = [path for path in source_files if path.name == "schema.py"]
    runtime_files = [path for path in source_files if path.name != "schema.py"]
    source_blob = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in runtime_files)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        relations = con.execute(
            """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema='main'
            ORDER BY table_type, table_name
            """
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for name, relation_type in relations:
            count = None
            if relation_type == "BASE TABLE":
                count = int(con.execute(f"SELECT count(*) FROM {_q(name)}").fetchone()[0])
            token = re.escape(str(name))
            refs = len(re.findall(rf"\b{token}\b", source_blob, flags=re.IGNORECASE))
            writes = len(
                re.findall(
                    rf"(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE[^\n]*?)\s+{token}\b",
                    source_blob,
                    flags=re.IGNORECASE,
                )
            )
            rows.append(
                {
                    "name": str(name),
                    "type": str(relation_type),
                    "rows": count,
                    "static_refs": refs,
                    "static_write_refs": writes,
                    "archive_named": str(name).lower().startswith("_dedupe_archive_"),
                }
            )
    finally:
        con.close()
    return {"rows": rows, "schema_definition_files": len(schema_files)}


def collect_pipeline_timing(db_path: Path) -> dict[str, Any]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        exists = con.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema='main' AND table_name='pipeline_task_audit'
            """
        ).fetchone()[0]
        if not exists:
            return {"available": False, "rows": [], "latest": []}
        rows = con.execute(
            """
            SELECT task_name,
                   count(*) AS runs,
                   sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                   sum(CASE WHEN status IN ('failed','blocked','aborted') THEN 1 ELSE 0 END) AS failed,
                   round(avg(duration_seconds), 3) AS avg_seconds,
                   round(quantile_cont(duration_seconds, 0.95), 3) AS p95_seconds,
                   round(max(duration_seconds), 3) AS max_seconds
            FROM pipeline_task_audit
            WHERE duration_seconds IS NOT NULL
            GROUP BY task_name
            ORDER BY avg_seconds DESC NULLS LAST, task_name
            """
        ).fetchall()
        columns = [item[0] for item in con.description]
        latest = con.execute(
            """
            SELECT run_id, trade_date, phase, task_name, status, duration_seconds, reason
            FROM pipeline_task_audit
            ORDER BY updated_at DESC
            LIMIT 30
            """
        ).fetchall()
        latest_columns = [item[0] for item in con.description]
    finally:
        con.close()
    return {
        "available": True,
        "rows": [dict(zip(columns, row)) for row in rows],
        "latest": [dict(zip(latest_columns, row)) for row in latest],
    }


def collect_lock_evidence(db_path: Path) -> dict[str, Any]:
    lock_path = db_path.with_name(f"{db_path.name}.pipeline.lock")
    if not lock_path.exists():
        return {"path": str(lock_path), "present": False, "age_seconds": None}
    age_seconds = max(0.0, (datetime.now().timestamp() - lock_path.stat().st_mtime))
    return {"path": str(lock_path), "present": True, "age_seconds": round(age_seconds, 3)}


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(result: dict[str, Any]) -> str:
    scripts = result["scripts"]
    relations = result["relations"]["rows"]
    timing = result["timing"]
    lock = result["lock"]
    lines = [
        "# Maintenance Surface Audit",
        "",
        f"- Generated at: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- Database: `{result['db']}`",
        "- Mode: read-only; no tables, files, or runtime state were changed.",
        f"- Pipeline lock present at audit time: `{lock['present']}`; path `{lock['path']}`; "
        f"age seconds `{lock['age_seconds'] if lock['age_seconds'] is not None else ''}`.",
        "",
        "## Script surface",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    for category, count in sorted(scripts["counts"].items()):
        lines.append(f"| {category} | {count} |")
    lines.extend(
        [
            "",
            "| Script | Category | Lifecycle | Imports trade_system | Mentioned by integrated runner | Lines |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for row in scripts["rows"]:
        if row["category"] in {"canonical_pipeline", "audit", "backfill", "runner_or_research"}:
            lines.append(
                f"| `{row['name']}` | {row['category']} | `{row['lifecycle']}` | {row['imports_trade_system']} | "
                f"{row['mentioned_by_integrated_runner']} | {row['lines']} |"
            )
    empty = [row for row in relations if row["type"] == "BASE TABLE" and row["rows"] == 0]
    unreferenced_empty = [row for row in empty if row["static_refs"] == 0]
    lines.extend(
        [
            "",
            "## Relation surface",
            "",
            f"- Base tables: `{sum(row['type'] == 'BASE TABLE' for row in relations)}`",
            f"- Views: `{sum(row['type'] == 'VIEW' for row in relations)}`",
            f"- Empty base tables: `{len(empty)}`",
            f"- Empty tables with no runtime source reference (schema.py excluded): `{len(unreferenced_empty)}`",
            f"- Named archive tables: `{sum(row['archive_named'] for row in relations)}`",
            "",
            "| Relation | Type | Rows | Runtime refs | Runtime write refs | Archive name |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(empty, key=lambda item: (-item["static_refs"], item["name"]))[:40]:
        lines.append(
            f"| `{row['name']}` | {row['type']} | {row['rows']} | {row['static_refs']} | "
            f"{row['static_write_refs']} | {row['archive_named']} |"
        )
    lines.extend(["", "## Pipeline timing", ""])
    if not timing["available"]:
        lines.append("- `pipeline_task_audit` is not available.")
    else:
        lines.extend(
            [
                "| Task | Runs | Completed | Failed/blocked | Avg sec | P95 sec | Max sec |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in timing["rows"][:40]:
            lines.append(
                f"| `{row['task_name']}` | {row['runs']} | {row['completed']} | {row['failed']} | "
                f"{row['avg_seconds']} | {row['p95_seconds']} | {row['max_seconds']} |"
            )
    lines.extend(
        [
            "",
            "## Decision rules",
            "",
            "- Empty is not the same as unused; the runtime source reference count is a triage signal only.",
            "- A task is not an in-process migration candidate until its writes, retry boundary, and lock behavior are measured.",
            "- Keep the current close path and compatibility scripts until a replacement passes the same-date readiness and artifact gates.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only maintenance surface audit.")
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "maintenance_surface_latest.md"))
    args = parser.parse_args()
    db_path = Path(args.db)
    source_files = _source_files(ROOT)
    result = {
        "db": str(db_path),
        "scripts": collect_script_catalog(ROOT),
        "relations": collect_relation_catalog(db_path, source_files),
        "timing": collect_pipeline_timing(db_path),
        "lock": collect_lock_evidence(db_path),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    relation_rows = result["relations"]["rows"]
    empty = sum(row["type"] == "BASE TABLE" and row["rows"] == 0 for row in relation_rows)
    print(
        f"MAINTENANCE_SURFACE scripts={len(result['scripts']['rows'])} "
        f"relations={len(relation_rows)} empty_tables={empty} out={out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
