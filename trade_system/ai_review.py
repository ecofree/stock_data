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


def _capability_summary(value: dict[str, Any]) -> dict[str, Any]:
    """Expose usage evidence without sending the entire market/feature pool."""
    fields = ('ready', 'scope', 'scope_sha256', 'expected_rows', 'qualified_rows',
              'input_received_at_min', 'input_received_at_max', 'breadth',
              'breadth_scope', 'blockers', 'model_id', 'feature_columns', 'usage')
    domains = value.get('capabilities', {})
    result = {key: value[key] for key in ('schema', 'trade_date', 'as_of') if key in value}
    result['capabilities'] = {}
    for name, fact in domains.items():
        if not isinstance(fact, dict):
            continue
        summary = {key: fact[key] for key in fields if key in fact}
        if name == 'flow_observation':
            for kind in ('stock', 'sector'):
                child = fact.get(kind) or {}
                summary[kind] = {key: child[key] for key in fields if key in child}
        result['capabilities'][name] = summary
    return result


def _evidence_catalog(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Create stable, human-readable evidence references for the AI layer."""
    trade_date = snapshot.get("trade_date")
    flow = snapshot.get("capital_flow", {}) or {}
    stock_meta = flow.get("stock_meta", {}) or {}
    sector_meta = flow.get("sector_meta", {}) or {}
    catalog = [
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
    # An ID is not evidence when its facts were withheld or are absent. In
    # particular, an unconfirmed provider observation cannot be cited through
    # an otherwise valid ID as an independently confirmed signal.
    result = []
    for item in catalog:
        value: Any = snapshot
        for part in item['path'].split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        if value:
            item['usage'] = (
                ['summary', 'risk_flags']
                if item['evidence_id'] in {'data_gate', 'market.breadth'}
                else ['summary', 'risk_flags', 'watchlist']
            )
            result.append(item)
    return result


def build_ai_review_snapshot(db_path: str | Path, trade_date: str | None = None) -> dict[str, Any]:
    context = build_daily_review_context(db_path, trade_date)
    flow = context.get("capital_flow", {}) or {}
    sources = context.get("data_sources", {}) or {}
    gate = context.get("readiness", {}) or context.get("p0_gate", {}) or {}
    execution_control = context.get("execution_control", {}) or {}
    capabilities = context.get('operational_capabilities') or gate.get('operational_capabilities') or {}
    domains = capabilities.get('capabilities', {}) if isinstance(capabilities, dict) else {}
    strict_analysis = (
        gate.get('data_certified_ready') is True
        and gate.get('flow_certified_ready') is True
        and gate.get('analysis_ready') is True
        and all(execution_control.get(key, gate.get(key)) is True for key in (
            'data_certified_ready', 'flow_certified_ready', 'analysis_ready',
        ))
    )
    market_facts = (
        capabilities.get('schema') == 'operational_capabilities_v1'
        and domains.get('market_view', {}).get('ready') is True
        and bool(domains.get('market_view', {}).get('breadth'))
    )
    selected_trade_date = context.get("trade_date") or trade_date
    qlib_candidates: list[dict[str, Any]] = []
    if strict_analysis:
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
        "execution_ready": False,
        'operational_capabilities': _capability_summary(capabilities),
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
            "execution_ready": False,
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
    if not strict_analysis:
        # Deterministic pages may show labelled provider observations. The AI
        # contract is narrower: do not supply unconfirmed flow or candidates
        # with unknown feature dependencies to its narrative/signal path.
        snapshot['capital_flow'] = {}
        snapshot['research'] = {}
        snapshot['market'] = {
            'breadth': domains['market_view']['breadth'] if market_facts else {},
            'breadth_scope': domains.get('market_view', {}).get('breadth_scope'),
            'scope_sha256': domains.get('market_view', {}).get('scope_sha256'),
        }
        snapshot['data_gate']['flow_features'] = {}
        snapshot['data_gate']['gate'] = {
            key: gate[key] for key in (
                'operator_state_version', 'operator_status', 'run_status',
                'source_ready', 'pipeline_ready', 'artifact_current',
                'data_certified_ready', 'flow_certified_ready', 'analysis_ready',
                'execution_ready', 'missing_groups', 'blockers', 'warnings',
            ) if key in gate
        }
        snapshot['ai_contract']['allowed'] = [
            'explain_input_limitations',
            *(['summarize_available_price_facts'] if market_facts else []),
        ]
        snapshot['ai_contract']['forbidden'] += [
            'infer_confirmed_capital_flow_from_provider_observation',
            'use_unknown_feature_candidates_as_price_only_research',
            'produce_next_session_watchlist_without_qualified_signal_evidence',
        ]
    snapshot['ai_contract']['scope'] = (
        'certified_analysis' if strict_analysis else
        'price_observation' if market_facts else 'data_limitations_only'
    )
    snapshot['ai_contract']['watchlist_allowed'] = strict_analysis
    snapshot['ai_contract']['evidence_rule'] += (
        ' Evidence usage is restricted per catalog entry. Price observations '
        'do not certify capital flow, valuation, a full market day or execution.'
    )
    snapshot["evidence_catalog"] = _evidence_catalog(snapshot)
    return snapshot
