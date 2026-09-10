"""Operator-side risk gates for manual trading plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from numbers import Real


WEAK_MARKET_REGIMES = {"退潮", "冰点", "weak", "risk_off"}


@dataclass(frozen=True)
class OperatorRiskConfig:
    max_total_position_pct: float = 20.0
    max_single_stock_pct: float = 10.0
    min_score: float = 70.0
    weak_regime_single_stock_pct: float = 5.0


@dataclass(frozen=True)
class TradePlanInput:
    stock_code: str
    score: float
    planned_position_pct: float
    current_total_position_pct: float | None
    market_regime: str
    risk_flags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    severity: str
    reason: str
    evidence: tuple[str, ...] = field(default_factory=tuple)


def evaluate_trade_plan(plan: TradePlanInput, config: OperatorRiskConfig | None = None) -> RiskDecision:
    cfg = config or OperatorRiskConfig()
    # Validate before arithmetic/comparisons: NaN makes both < and > false.
    values = {
        "score": plan.score,
        "planned_position_pct": plan.planned_position_pct,
        "current_total_position_pct": plan.current_total_position_pct,
        "max_total_position_pct": cfg.max_total_position_pct,
        "max_single_stock_pct": cfg.max_single_stock_pct,
        "min_score": cfg.min_score,
        "weak_regime_single_stock_pct": cfg.weak_regime_single_stock_pct,
    }
    invalid = tuple(
        name for name, value in values.items()
        if isinstance(value, bool) or not isinstance(value, Real)
        or not isfinite(value) or not 0 <= value <= 100
    )
    if invalid:
        return RiskDecision(False, "P0", "invalid risk input or policy", invalid)
    projected_total = plan.current_total_position_pct + plan.planned_position_pct

    if plan.planned_position_pct <= 0:
        return RiskDecision(
            False,
            "P0",
            "planned position must be positive",
            (f"planned={plan.planned_position_pct:.2f}",),
        )
    if projected_total > cfg.max_total_position_pct:
        return RiskDecision(
            False,
            "P0",
            "total position limit exceeded",
            (f"projected_total={projected_total:.2f}", f"limit={cfg.max_total_position_pct:.2f}"),
        )
    if plan.planned_position_pct > cfg.max_single_stock_pct:
        return RiskDecision(
            False,
            "P0",
            "single stock position limit exceeded",
            (f"planned={plan.planned_position_pct:.2f}", f"limit={cfg.max_single_stock_pct:.2f}"),
        )
    if plan.score < cfg.min_score:
        return RiskDecision(
            False,
            "P1",
            "candidate score below minimum",
            (f"score={plan.score:.2f}", f"minimum={cfg.min_score:.2f}"),
        )
    if plan.market_regime in WEAK_MARKET_REGIMES and plan.planned_position_pct > cfg.weak_regime_single_stock_pct:
        return RiskDecision(
            False,
            "P1",
            "weak market regime requires reduced probing position",
            (
                f"market_regime={plan.market_regime}",
                f"planned={plan.planned_position_pct:.2f}",
                f"weak_limit={cfg.weak_regime_single_stock_pct:.2f}",
            ),
        )
    if plan.risk_flags:
        return RiskDecision(
            False,
            "P1",
            "manual risk flags require review",
            tuple(plan.risk_flags),
        )
    return RiskDecision(True, "OK", "plan passed operator risk checks")
