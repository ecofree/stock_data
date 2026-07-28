"""Strategy scan engine for staged operator candidates."""

from __future__ import annotations

import json
from typing import Any

from trade_system.strategy.definition import StrategyDefinition


STAGE_ALIASES = {
    "premarket_pool": "pre_market",
    "pre_market": "pre_market",
    "auction_confirmation": "auction_confirm",
    "auction_confirm": "auction_confirm",
    "intraday_strength": "intraday_strength",
    "close_decision": "closing_decision",
    "closing_decision": "closing_decision",
}


def _candidate_stage(candidate: dict[str, Any]) -> str:
    raw_stage = str(candidate.get("stage") or candidate.get("operator_stage") or "")
    return STAGE_ALIASES.get(raw_stage, raw_stage)


def _candidate_score(candidate: dict[str, Any], strategy: StrategyDefinition) -> float:
    value = candidate.get(strategy.score_field, candidate.get("score", 0))
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def run_strategy_scan(candidates: list[dict[str, Any]], strategies: list[StrategyDefinition]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        stage = _candidate_stage(candidate)
        for strategy in strategies:
            if stage != strategy.stage:
                continue
            score = _candidate_score(candidate, strategy)
            if score < strategy.min_score:
                continue
            evidence = {
                "score": score,
                "min_score": strategy.min_score,
                "stage": stage,
                "entry_rules": strategy.entry_rules,
                "candidate": {
                    key: candidate.get(key)
                    for key in ("sector_strength", "decision", "data_origin")
                    if key in candidate
                },
            }
            symbol = candidate.get("stock_code") or candidate.get("symbol")
            results.append(
                {
                    "trade_date": str(candidate.get("trade_date") or candidate.get("date") or ""),
                    "strategy_id": strategy.strategy_id,
                    "symbol": str(symbol or ""),
                    "stock_name": str(candidate.get("stock_name") or candidate.get("name") or ""),
                    "stage": strategy.stage,
                    "score": score,
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    "selected_reason": "；".join(strategy.entry_rules),
                    "risk_points": "；".join(strategy.risk_rules),
                    "invalid_conditions": "；".join(strategy.invalid_conditions),
                }
            )
    return results
