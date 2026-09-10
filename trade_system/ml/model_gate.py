"""Fail-closed QLib model promotion checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables


DEFAULT_GATES = {
    "min_samples": 5000,
    "min_rank_ic": 0.02,
    "min_net_spread": 0.0,
    "min_avg_net_return": 0.0,
    "max_drawdown_pct": 15.0,
}


def audit_model_promotion(
    db_path: str | Path,
    *,
    model_id: str | None = None,
    gates: dict[str, float] | None = None,
    promote: bool = False,
) -> dict[str, Any]:
    """Audit models and optionally promote one only when every gate passes.

    Promotion is an explicit action; an audit alone never changes signal
    impact.  The caller must still pass a model that has a current evaluation.
    """
    ensure_qlib_shadow_tables(db_path)
    thresholds = {**DEFAULT_GATES, **(gates or {})}
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        where = "WHERE r.model_id = ?" if model_id else ""
        params = [model_id] if model_id else []
        rows = con.execute(
            f"""
            SELECT r.model_id, r.status, r.model_file_ref, r.predict_horizon,
                   e.sample_count, e.rank_ic, e.avg_net_return,
                   coalesce(e.net_top_bottom_spread, e.top_bottom_spread) AS net_spread,
                   e.max_drawdown, e.updated_at
            FROM qlib_model_registry r
            LEFT JOIN qlib_shadow_evaluation e ON e.model_id = r.model_id
            {where}
            ORDER BY r.model_id
            """,
            params,
        ).fetchall()
        audited = []
        for row in rows:
            item = dict(zip(
                ("model_id", "status", "model_file_ref", "predict_horizon", "sample_count", "rank_ic", "avg_net_return", "net_spread", "max_drawdown", "updated_at"),
                row,
            ))
            reasons: list[str] = []
            if item["status"] not in {"shadow", "challenger", "champion"}:
                reasons.append("model status is not promotable")
            if not item["model_file_ref"] or not Path(str(item["model_file_ref"])).exists():
                reasons.append("model artifact is missing")
            if item["sample_count"] is None or float(item["sample_count"]) < thresholds["min_samples"]:
                reasons.append("insufficient evaluated samples")
            if item["rank_ic"] is None or float(item["rank_ic"]) < thresholds["min_rank_ic"]:
                reasons.append("rank_ic below gate")
            if item["avg_net_return"] is None or float(item["avg_net_return"]) <= thresholds["min_avg_net_return"]:
                reasons.append("net return is not positive")
            if item["net_spread"] is None or float(item["net_spread"]) <= thresholds["min_net_spread"]:
                reasons.append("net top-bottom spread is not positive")
            if item["max_drawdown"] is None or float(item["max_drawdown"]) < -abs(thresholds["max_drawdown_pct"]):
                reasons.append("drawdown exceeds gate")
            item["eligible"] = not reasons
            item["reasons"] = reasons
            audited.append(item)

        promoted = None
        if promote:
            candidates = [item for item in audited if item["eligible"]]
            if len(candidates) != 1:
                raise ValueError("explicit promotion requires exactly one eligible model")
            promoted = candidates[0]["model_id"]
            con.execute("UPDATE qlib_model_registry SET status = 'shadow' WHERE status = 'champion'")
            con.execute("UPDATE qlib_model_registry SET status = 'champion' WHERE model_id = ?", [promoted])
        return {
            "gates": thresholds,
            "promote_requested": bool(promote),
            "promoted_model_id": promoted,
            "models": audited,
            "champion": next((item["model_id"] for item in audited if item["status"] == "champion"), None),
        }
    finally:
        con.close()

