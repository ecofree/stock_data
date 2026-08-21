"""AI-ready daily review facts.

This module deliberately does not call a remote model.  It creates a compact,
source-traceable JSON contract that an optional local/remote LLM can consume.
The deterministic review remains complete when no model or network is
available, and the snapshot cannot change execution readiness.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from trade_system.daily_review import build_daily_review_context


AI_REVIEW_SCHEMA_VERSION = "ai_review_facts_v1"


def _rows(context: dict[str, Any], key: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = context.get("capital_flow", {}).get(key, []) or []
    return [dict(row) for row in rows[:limit]]


def _evidence_catalog(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Create stable, human-readable evidence references for the AI layer."""
    trade_date = snapshot.get("trade_date")
    flow = snapshot.get("capital_flow", {}) or {}
    stock_meta = flow.get("stock_meta", {}) or {}
    sector_meta = flow.get("sector_meta", {}) or {}
    return [
        {"evidence_id": "data_gate", "path": "data_gate", "trade_date": trade_date, "source": "readiness_gate"},
        {"evidence_id": "market.regime", "path": "market.regime", "trade_date": trade_date, "source": "market_state"},
        {"evidence_id": "market.breadth", "path": "market.breadth", "trade_date": trade_date, "source": "market_daily"},
        {"evidence_id": "market.auction", "path": "market.auction", "trade_date": trade_date, "source": "auction_evidence"},
        {"evidence_id": "flow.stock_inflow_top50", "path": "capital_flow.stock_inflow_top50", "trade_date": trade_date, "source": stock_meta.get("providers"), "coverage": stock_meta.get("batch_coverage_pct")},
        {"evidence_id": "flow.stock_outflow_top50", "path": "capital_flow.stock_outflow_top50", "trade_date": trade_date, "source": stock_meta.get("providers"), "coverage": stock_meta.get("batch_coverage_pct")},
        {"evidence_id": "flow.sector_inflow_top10", "path": "capital_flow.sector_inflow_top10", "trade_date": trade_date, "source": sector_meta.get("taxonomy"), "coverage": sector_meta.get("batch_coverage_pct")},
        {"evidence_id": "flow.sector_outflow_top10", "path": "capital_flow.sector_outflow_top10", "trade_date": trade_date, "source": sector_meta.get("taxonomy"), "coverage": sector_meta.get("batch_coverage_pct")},
        {"evidence_id": "flow.stock_persistence", "path": "capital_flow.stock_persistence", "trade_date": trade_date, "source": stock_meta.get("providers"), "coverage": stock_meta.get("batch_coverage_pct")},
        {"evidence_id": "flow.sector_persistence", "path": "capital_flow.sector_persistence", "trade_date": trade_date, "source": sector_meta.get("taxonomy"), "coverage": sector_meta.get("batch_coverage_pct")},
        {"evidence_id": "flow.limit_up_concepts", "path": "capital_flow.limit_up_concepts", "trade_date": trade_date, "source": sector_meta.get("taxonomy"), "coverage": sector_meta.get("batch_coverage_pct")},
        {"evidence_id": "research.candidates", "path": "research.candidates", "trade_date": trade_date, "source": "deterministic_candidate_pool"},
        {"evidence_id": "research.qlib_candidate_pool", "path": "research.qlib_candidate_pool", "trade_date": trade_date, "source": "qlib_candidate_pool"},
        {"evidence_id": "research.qlib_shadow", "path": "research.qlib_shadow", "trade_date": trade_date, "source": "qlib_shadow_evaluation"},
        {"evidence_id": "research.strategy", "path": "research.strategy", "trade_date": trade_date, "source": "strategy_backtest"},
    ]


def build_ai_review_snapshot(db_path: str | Path, trade_date: str | None = None) -> dict[str, Any]:
    context = build_daily_review_context(db_path, trade_date)
    flow = context.get("capital_flow", {}) or {}
    sources = context.get("data_sources", {}) or {}
    gate = context.get("readiness", {}) or context.get("p0_gate", {}) or {}
    execution_control = context.get("execution_control", {}) or {}
    selected_trade_date = context.get("trade_date") or trade_date
    qlib_candidates: list[dict[str, Any]] = []
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name='qlib_candidate_pool'"
        ).fetchone()[0]
        if exists:
            cur = con.execute(
                """
                SELECT stock_code, stock_name, model_id, combined_score,
                       model_status, candidate_status, risk_approved, is_executable,
                       blocker_reason, evidence_json
                FROM qlib_candidate_pool
                WHERE trade_date = ?
                ORDER BY combined_score DESC, stock_code
                LIMIT 50
                """,
                [selected_trade_date],
            )
            columns = [desc[0] for desc in cur.description]
            qlib_candidates = [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        con.close()
    snapshot = {
        "schema_version": AI_REVIEW_SCHEMA_VERSION,
        "trade_date": context.get("trade_date") or trade_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "analysis_only": True,
        "execution_ready": bool(execution_control.get("execution_ready", False)),
        "data_gate": {
            "source_ready": execution_control.get("source_ready"),
            "pipeline_ready": execution_control.get("pipeline_ready"),
            "artifact_current": execution_control.get("artifact_current"),
            "data_certified_ready": execution_control.get("data_certified_ready", gate.get("data_certified_ready")),
            "flow_certified_ready": execution_control.get("flow_certified_ready", gate.get("flow_certified_ready")),
            "analysis_ready": execution_control.get("analysis_ready", gate.get("analysis_ready")),
            "operator_status": execution_control.get("operator_status", gate.get("operator_status")),
            "certified_ready": execution_control.get("certified_ready"),
            "analytics_ready": execution_control.get("analytics_ready"),
            "execution_ready": execution_control.get("execution_ready"),
            "gate": gate,
            "tushare": sources.get("tushare", []),
            "kline": sources.get("kline", []),
            "ths": sources.get("ths", {}),
            "flow_features": sources.get("flow_features", {}),
        },
        "market": {
            "regime": context.get("regime", {}),
            "breadth": (context.get("market_context", {}) or {}).get("breadth", []),
            "auction": (context.get("market_context", {}) or {}).get("auction", []),
        },
        "capital_flow": {
            "stock_meta": flow.get("stock_flow_meta", {}),
            "sector_meta": flow.get("sector_flow_meta", {}),
            "stock_inflow_top50": _rows(context, "stock_inflow", 50),
            "stock_outflow_top50": _rows(context, "stock_outflow", 50),
            "sector_inflow_top10": _rows(context, "sector_inflow", 10),
            "sector_outflow_top10": _rows(context, "sector_outflow", 10),
            "stock_persistence": _rows(context, "stock_flow_persistence", 50),
            "sector_persistence": _rows(context, "sector_flow_persistence", 50),
            "limit_up_concepts": _rows(context, "sector_limit_up", 50),
        },
        "research": {
            "candidates": _rows(context, "candidate_picks", 20),
            "qlib_candidate_pool": qlib_candidates,
            "qlib_shadow": sources.get("qlib", []),
            "strategy": sources.get("strategy", []),
            "operator_outcomes": sources.get("outcomes", 0),
        },
        "ai_contract": {
            "status": "facts_ready_no_model_call",
            "allowed": [
                "summarize_factual_flow_changes",
                "explain_stock_sector_divergence",
                "identify_anomalies_and_risks",
                "produce_next_session_watchlist",
            ],
            "forbidden": [
                "invent_missing_values",
                "change_data_quality_or_execution_gate",
                "turn_shadow_score_into_order",
                "claim_live_performance_without_operator_outcome",
            ],
            "evidence_rule": "Every numeric statement must cite trade_date, provider, coverage and source table from this snapshot.",
        },
    }
    snapshot["evidence_catalog"] = _evidence_catalog(snapshot)
    return snapshot
