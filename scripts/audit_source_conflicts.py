"""Audit same-key provider conflicts without changing the live database."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.source_authority import SOURCE_POLICIES, provider_rank


def _q(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        str(row[0]) for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }


def _duplicate_groups(
    con: duckdb.DuckDBPyConnection,
    table: str,
    key_columns: tuple[str, ...],
    *,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    # DuckDB PRAGMA table_info returns (cid, name, type, ...); using cid here
    # made every provider-aware table look schema-less and caused this audit
    # to report zero counts/conflicts even when millions of rows existed.
    columns = {str(row[1]) for row in con.execute(f"PRAGMA table_info({_q(table)})").fetchall()}
    if not set(key_columns) | {"provider"} <= columns:
        return []
    key_sql = ", ".join(_q(column) for column in key_columns)
    rows = con.execute(
        f"SELECT {key_sql}, count(*) AS row_count, count(DISTINCT provider) AS provider_count, "
        f"string_agg(DISTINCT coalesce(provider, '<null>'), ', ' ORDER BY coalesce(provider, '<null>')) AS providers "
        f"FROM {_q(table)} WHERE coalesce(is_stale, FALSE)=FALSE "
        f"GROUP BY {key_sql} HAVING count(DISTINCT provider)>1 ORDER BY row_count DESC LIMIT ?",
        [limit],
    ).fetchall()
    names = list(key_columns) + ["row_count", "provider_count", "providers"]
    return [dict(zip(names, row)) for row in rows]


def _provider_counts(con: duckdb.DuckDBPyConnection, table: str) -> list[dict[str, Any]]:
    columns = {str(row[1]) for row in con.execute(f"PRAGMA table_info({_q(table)})").fetchall()}
    if "provider" not in columns:
        return []
    rows = con.execute(
        f"SELECT coalesce(provider, '<null>'), count(*) FROM {_q(table)} GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()
    return [{"provider": str(row[0]), "rows": int(row[1])} for row in rows]


def audit(db_path: str | Path, *, limit: int = 1000) -> dict[str, Any]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = _tables(con)
        domains = {
            "kline": ("multi_source_kline", ("source_date", "asset_type", "asset_code")),
            "stock_flow": ("multi_source_stock_flow", ("source_date", "stock_code")),
            "sector_flow": ("multi_source_sector_flow", ("source_date", "sector_code")),
        }
        conflicts: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, list[dict[str, Any]]] = {}
        for domain, (table, keys) in domains.items():
            if table in tables:
                conflicts[domain] = _duplicate_groups(con, table, keys, limit=limit)
                counts[domain] = _provider_counts(con, table)
            else:
                conflicts[domain] = []
                counts[domain] = []

        ths_duplicates: dict[str, int] = {}
        for table, keys in (
            ("ths_concept_daily", ("trade_date", "concept_code")),
            ("ths_concept_stock_history", ("trade_date", "concept_code", "stock_code")),
        ):
            if table not in tables:
                continue
            key_sql = ", ".join(_q(column) for column in keys)
            ths_duplicates[table] = int(con.execute(
                f"SELECT count(*) FROM (SELECT {key_sql} FROM {_q(table)} GROUP BY {key_sql} HAVING count(*)>1)"
            ).fetchone()[0])
        return {
            "db": str(Path(db_path).resolve()),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "conflicts": conflicts,
            "provider_counts": counts,
            "ths_duplicate_groups": ths_duplicates,
            "policies": list(SOURCE_POLICIES),
        }
    finally:
        con.close()


def render(result: dict[str, Any]) -> str:
    lines = [
        "# Source Conflict Audit",
        "",
        f"- Generated at: `{result['generated_at']}`",
        f"- Database: `{result['db']}`",
        "- Mode: read-only; no database rows or files were changed.",
        "",
        "## Provider counts",
        "",
        "| Domain | Provider | Rows | Declared rank |",
        "|---|---|---:|---:|",
    ]
    for domain, rows in result["provider_counts"].items():
        for row in rows:
            rank = provider_rank(domain, row["provider"]) if domain in SOURCE_POLICIES else ""
            lines.append(f"| `{domain}` | `{row['provider']}` | {row['rows']} | {rank} |")
    lines.extend([
        "",
        "## Same-key multi-provider groups",
        "",
        "A group is a conflict candidate when the same business key has more than one live provider. It is not automatically an error; the declared authority policy decides which row may be promoted.",
        "",
        "| Domain | Key | Rows | Providers | Declared primary |",
        "|---|---|---:|---|---|",
    ])
    for domain, rows in result["conflicts"].items():
        policy = SOURCE_POLICIES.get(domain)
        for row in rows:
            keys = ", ".join(str(row[key]) for key in ("source_date", "asset_type", "asset_code", "stock_code", "sector_code") if key in row)
            primary = ", ".join(policy.primary) if policy else ""
            lines.append(f"| `{domain}` | `{keys}` | {row['row_count']} | `{row['providers']}` | {primary} |")
    lines.extend([
        "",
        "## THS duplicate groups",
        "",
        "| Table | Duplicate business-key groups |",
        "|---|---:|",
    ])
    for table, count in result["ths_duplicate_groups"].items():
        lines.append(f"| `{table}` | {count} |")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- Provider-separated rows are retained for evidence and reconciliation.",
        "- Core promotion must use the authority matrix, not latest `fetched_at` alone.",
        "- A non-zero conflict count requires checking provider, unit, taxonomy, as-of date, and write owner before cleanup.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit same-key provider conflicts.")
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "source_conflict_audit_latest.md"))
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    result = audit(args.db, limit=max(1, int(args.limit)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(result), encoding="utf-8")
    total = sum(len(rows) for rows in result["conflicts"].values())
    print(f"SOURCE_CONFLICT_AUDIT groups={total} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
