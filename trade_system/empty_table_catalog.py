"""Classify empty DuckDB tables for future data-source work."""

from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime

import duckdb

from trade_system.quality import table_exists
from trade_system.db_utils import fetch_dicts as _fetch_dicts
from trade_system.maintenance_registry import empty_table_collection_plan, empty_table_decision
from trade_system.api_endpoint_inventory import table_to_endpoint


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


# Current live audit facts that must override stale endpoint metadata left by
# an earlier KPL host. These overrides are intentionally narrow; all other
# tables continue to use the installed endpoint inventory.
CURRENT_ROUTE_OVERRIDES = {
    "auction_tick": {
        "endpoint": "/auction/market",
        "verdict": "stable_available",
        "usefulness": "professional_core",
    },
    "advanced_morning_bidding_list": {
        "endpoint": "/advanced/morning-bidding-list",
        "verdict": "permission_denied",
        "usefulness": "professional_useful",
    },
    "advanced_morning_bidding_summary": {
        "endpoint": "/advanced/morning-bidding-summary",
        "verdict": "permission_denied",
        "usefulness": "professional_useful",
    },
    "advanced_fenbi2": {
        "endpoint": "/advanced/fenbi2",
        "verdict": "permission_denied",
        "usefulness": "professional_useful",
    },
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


def _endpoint_rows(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    if not table_exists(con, "api_endpoint_inventory"):
        return {}
    return {
        str(row["endpoint"]): row
        for row in _fetch_dicts(
            con,
            "SELECT endpoint, table_name, verdict, usefulness "
            "FROM api_endpoint_inventory WHERE endpoint IS NOT NULL AND endpoint != ''",
        )
    }


def _official_audit_rows(path: str | Path | None) -> dict[str, dict]:
    """Load the latest explicit KPL audit without making catalog collection live."""
    if not path:
        return {}
    audit_path = Path(path)
    if not audit_path.exists():
        return {}
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result = {}
    for row in payload.get("rows", []):
        route = str(row.get("route") or row.get("endpoint") or "").strip()
        if route.startswith("/api/"):
            route = route[4:]
        if not route:
            continue
        verdict = str(row.get("verdict") or "").strip().lower()
        mapped = {
            "usable_with_data": "stable_available",
            "reachable_empty": "reachable_empty",
            "permission_denied": "permission_denied",
            "not_found_or_retired": "api_error",
            "transport_error": "api_error",
            "server_error": "api_error",
        }.get(verdict, verdict)
        result[route] = {
            "endpoint": route,
            "verdict": mapped,
            "usefulness": "professional_core" if str(row.get("category")) in {"core", "important"} else "professional_useful",
        }
    return result


def _snapshot_is_fresh(path: str | Path | None, *, max_age_hours: int = 72) -> bool:
    if not path:
        return False
    try:
        age_seconds = (datetime.now() - datetime.fromtimestamp(Path(path).stat().st_mtime)).total_seconds()
    except OSError:
        return False
    return 0 <= age_seconds <= max_age_hours * 3600


def _probe_run_is_fresh(con: duckdb.DuckDBPyConnection, *, max_age_hours: int = 72) -> bool:
    if not table_exists(con, "api_endpoint_probe_run"):
        return False
    try:
        row = con.execute(
            "SELECT max(completed_at) FROM api_endpoint_probe_run WHERE status='completed'"
        ).fetchone()
    except Exception:
        return False
    if not row or not row[0]:
        return False
    return (datetime.now() - row[0].replace(tzinfo=None)).total_seconds() <= max_age_hours * 3600


def _classify(table_name: str, endpoint: dict | None, *, capability_fresh: bool = True) -> str:
    if table_name in WORKFLOW_TABLES:
        return "workflow_not_used"
    if table_name.startswith(EXTERNAL_TABLE_PREFIXES):
        return "external_missing"
    if endpoint:
        if not capability_fresh:
            return "capability_stale"
        verdict = endpoint.get("verdict")
        if verdict == "permission_denied":
            return "permission_denied"
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
    if classification == "capability_stale":
        return "reprobe_capability_snapshot"
    if classification == "reachable_empty":
        return "probe_then_fallback"
    if classification == "param_uncertain":
        return "parameter_review"
    if classification == "permission_denied":
        return "no_live_gate"
    return "no_live_gate"


def build_empty_table_catalog(
    db_path: str | Path,
    *,
    official_audit_path: str | Path | None = None,
) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        endpoints = _endpoint_map(con)
        endpoint_rows = _endpoint_rows(con)
        current_endpoint_rows = _official_audit_rows(official_audit_path)
        active_overrides = CURRENT_ROUTE_OVERRIDES if official_audit_path else {}
        capability_fresh = _snapshot_is_fresh(official_audit_path) or _probe_run_is_fresh(con)
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
            endpoint = active_overrides.get(table_name)
            if endpoint is None:
                endpoint = endpoints.get(table_name)
            if endpoint is None:
                route = table_to_endpoint(table_name)
                endpoint = (
                    current_endpoint_rows.get(route)
                    or endpoint_rows.get(route)
                    if route
                    else None
                )
            elif endpoint.get("endpoint") in current_endpoint_rows:
                # The explicit live audit wins over an older installed row,
                # while the narrow route overrides above remain strongest.
                endpoint = current_endpoint_rows[endpoint["endpoint"]]
            classification = _classify(table_name, endpoint, capability_fresh=capability_fresh)
            usefulness = endpoint.get("usefulness", "") if endpoint else ""
            collection_plan = empty_table_collection_plan(
                table_name, classification, usefulness
            )
            items.append(
                {
                    "table_name": table_name,
                    "classification": classification,
                    "endpoint": endpoint.get("endpoint") if endpoint else "",
                    "verdict": endpoint.get("verdict") if endpoint else "",
                    "usefulness": usefulness,
                    "action": _action(classification, usefulness),
                    **collection_plan,
                    **empty_table_decision(table_name, classification),
                }
            )
    finally:
        con.close()
    summary: dict[str, int] = {}
    for item in items:
        summary[item["classification"]] = summary.get(item["classification"], 0) + 1
    return {
        "summary": summary,
        "items": items,
        "capability_snapshot": {
            "path": str(official_audit_path) if official_audit_path else "",
            "fresh": capability_fresh,
            "max_age_hours": 72,
        },
    }


def render_empty_table_catalog(catalog: dict) -> str:
    lines = [
        "# Empty Table Catalog",
        "",
        "## Summary",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]
    snapshot = catalog.get("capability_snapshot") or {}
    for name, count in sorted(catalog.get("summary", {}).items()):
        lines.append(f"| `{name}` | {count} |")
    lines.extend([
        "",
        f"- Capability snapshot fresh (<= {snapshot.get('max_age_hours', 72)}h): `{str(bool(snapshot.get('fresh'))).lower()}`",
        f"- Capability snapshot path: `{snapshot.get('path') or 'none'}`",
    ])
    lines.extend(
        [
            "",
            "## Empty Tables",
            "",
            "| Table | Classification | Priority | Collection profile | Cadence | Retention | Decision | Review days | Endpoint | Verdict | Usefulness | Action |",
            "|---|---|---|---|---|---|---|---|---:|---|---|---|---|",
        ]
    )
    for item in catalog.get("items", []):
        lines.append(
            f"| `{item['table_name']}` | `{item['classification']}` | `{item.get('priority', '')}` | "
            f"`{item.get('collection_profile', '')}` | `{item.get('cadence', '')}` | `{item['retention']}` | "
            f"`{item['decision']}` | {item['review_days']} | {item.get('endpoint', '')} | "
            f"{item.get('verdict', '')} | {item.get('usefulness', '')} | {item.get('action', '')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `api_available_not_collected`: API probe says data is available, but the local table is empty.",
            "- `needs_trading_session`: rerun during the relevant market session before calling it missing.",
            "- `external_missing`: requires qlib, finance, or another non-KPL source.",
            "- `permission_denied`: the official route is reachable but not enabled for this key; it is never a production gate.",
            "- `capability_stale`: endpoint metadata exists but no live KPL capability snapshot is fresh; reprobe before deciding to collect or defer.",
            "- `workflow_not_used`: table is populated only after operator planning/review workflow runs.",
            "- `P1/review_supplement`: bounded after-close review batch; it does not block the close publication gate.",
            "- `P2/professional_optional`: weekly or on-demand professional data; endpoint availability alone does not authorize daily collection.",
            "- `collection_reason`: why an empty table is currently deferred, so `api_available_not_collected` is not mistaken for an accidental omission.",
            "",
        ]
    )
    return "\n".join(lines)
