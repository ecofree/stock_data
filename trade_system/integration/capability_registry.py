"""Capability registry rows derived from Phase 11 external audit decisions."""

from __future__ import annotations

from typing import Any


OWNER_LAYER_BY_TYPE = {
    "adapter": "data",
    "auction": "signal",
    "backtest": "backtest",
    "data": "data",
    "execution": "forbidden",
    "indicator": "strategy",
    "ml_shadow": "ml_shadow",
    "monitor": "risk",
    "research": "research",
    "review": "review",
    "risk": "risk",
    "secret": "forbidden",
    "service": "reference",
    "signal": "signal",
    "strategy": "strategy",
    "tooling": "tooling",
    "ui": "ui",
}


def build_capability_registry_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in audit.get("capability_matrix", []):
        capability_type = item.get("capability_type", "")
        rows.append(
            {
                "capability_id": item["capability_id"],
                "project": item["project"],
                "module_path": item["module_path"],
                "capability_type": capability_type,
                "decision": item["decision"],
                "target_adapter": item.get("target_adapter", ""),
                "blocked_reason": item.get("blocked_reason", ""),
                "test_required": item["decision"] in {"port", "reference"},
                "owner_layer": OWNER_LAYER_BY_TYPE.get(capability_type, "unknown"),
                "exists": bool(item.get("exists", False)),
            }
        )
    return rows
