"""Classify empty DuckDB tables for future data-source work."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.quality import table_exists
from trade_system.db_utils import fetch_dicts as _fetch_dicts


EXTERNAL_TABLE_PREFIXES = ("qlib", "finance")
WORKFLOW_TABLES = {
    "watchlist",
    "trade_plan",
    "portfolio_snapshot",
    "trade_journal",
    "operator_trade_outcome",
    "risk_snapshot",
    "research_note",
}



def _endpoint_map(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    if not table_exists(con, "api_endpoint_inventory"):
        return {}
    rows = _fetch_dicts(
        con,
        """
        SELECT endpoint, table_name, verdict, usefulness
        FROM api_endpoint_inventory
        WHERE table_name IS NOT NULL AND table_name != ''
        """,
    )
    result = {}
    for row in rows:
        result.setdefault(row["table_name"], row)
    return result


def _classify(table_name: str, endpoint: dict | None) -> str:
    if table_name in WORKFLOW_TABLES:
        return "workflow_not_used"
    if table_name.startswith(EXTERNAL_TABLE_PREFIXES):
        return "external_missing"
    if endpoint:
        verdict = endpoint.get("verdict")
        if verdict in {"stable_available", "api_available"}:
            return "api_available_not_collected"
        if verdict == "needs_trading_session":
            return "needs_trading_session"
        if verdict == "param_uncertain":
            return "param_uncertain"
        if verdict == "api_error":
            return "api_error"
        if verdict == "reachable_empty":
            return "reachable_empty"
    if table_name.startswith(("topic", "forums", "comments")):
        return "low_priority"
    return "intentional_or_unclassified"


def _action(classification: str, usefulness: str) -> str:
    if classification == "needs_trading_session":
        return "collect_in_phase"
    if classification == "workflow_not_used":
        return "operator_input_required"
    if classification == "external_missing":
        return "optional_dependency"
    if classification == "api_available_not_collected":
        return "bounded_off_hours_batch" if usefulness == "professional_core" else "defer_optional"
    if classification == "reachable_empty":
        return "probe_then_fallback"
    if classification == "param_uncertain":
        return "parameter_review"
    return "no_live_gate"


def build_empty_table_catalog(db_path: str | Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        endpoints = _endpoint_map(con)
        tables = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name NOT LIKE 'v_%' ORDER BY table_name"
            ).fetchall()
        ]
        items = []
        for table_name in tables:
            rows = int(con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0])
            if rows:
                continue
            endpoint = endpoints.get(table_name)
            classification = _classify(table_name, endpoint)
            usefulness = endpoint.get("usefulness", "") if endpoint else ""
            items.append(
                {
                    "table_name": table_name,
                    "classification": classification,
                    "endpoint": endpoint.get("endpoint") if endpoint else "",
                    "verdict": endpoint.get("verdict") if endpoint else "",
                    "usefulness": usefulness,
                    "action": _action(classification, usefulness),
                }
            )
    finally:
        con.close()
    summary: dict[str, int] = {}
    for item in items:
        summary[item["classification"]] = summary.get(item["classification"], 0) + 1
    return {"summary": summary, "items": items}


def render_empty_table_catalog(catalog: dict) -> str:
    lines = [
        "# Empty Table Catalog",
        "",
        "## Summary",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]
    for name, count in sorted(catalog.get("summary", {}).items()):
        lines.append(f"| `{name}` | {count} |")
    lines.extend(
        [
            "",
            "## Empty Tables",
            "",
            "| Table | Classification | Endpoint | Verdict | Usefulness | Action |",
            "|---|---|---|---|---|---|",
        ]
    )
    for item in catalog.get("items", []):
        lines.append(
            f"| `{item['table_name']}` | `{item['classification']}` | "
            f"{item.get('endpoint', '')} | {item.get('verdict', '')} | {item.get('usefulness', '')} | {item.get('action', '')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `api_available_not_collected`: API probe says data is available, but the local table is empty.",
            "- `needs_trading_session`: rerun during the relevant market session before calling it missing.",
            "- `external_missing`: requires qlib, finance, or another non-KPL source.",
            "- `workflow_not_used`: table is populated only after operator planning/review workflow runs.",
            "",
        ]
    )
    return "\n".join(lines)
