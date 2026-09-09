"""Versioned review metrics and the historical-validation gate.

This module deliberately does not read DuckDB or render HTML. It owns the
names, formulas and validation semantics of review metrics so daily, weekly
and monthly renderers cannot silently invent different composite-score rules.

The theme composite score is experimental until historical validation and
operator approval are both complete. A numeric score alone never proves
production readiness.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


METRIC_CONTRACT_VERSION = "review-metrics-v1"
COMPOSITE_SCORE_STATUS = "experimental_unvalidated"


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    unit: str
    source: str
    formula: str
    status: str = "canonical"


METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        "theme_breadth", "题材广度", "count",
        "same-date deduplicated limit-up pool + THS membership",
        "distinct limit-up stocks belonging to the concept",
    ),
    MetricDefinition(
        "theme_height", "题材高度", "board",
        "same-date canonical limit-up pool",
        "maximum verified consecutive board level among the concept's limit-up stocks",
    ),
    MetricDefinition(
        "theme_persistence", "题材持续性", "days",
        "versioned same-date concept snapshots",
        "number of valid trading days with concept limit-up breadth in the evaluation window",
    ),
    MetricDefinition(
        "theme_flow", "题材资金", "yuan",
        "canonical THS concept capital-flow snapshot",
        "same-date concept main-net inflow after source/unit normalization",
    ),
    MetricDefinition(
        "theme_composite_score", "题材综合分", "0-100",
        "the four metrics above",
        "35% breadth + 25% height + 20% persistence + 20% flow, each cross-sectional percentile",
        status=COMPOSITE_SCORE_STATUS,
    ),
)


_WEIGHTS = {"breadth": 0.35, "height": 0.25, "persistence": 0.20, "flow": 0.20}


def metric_contract() -> dict[str, Any]:
    """Return a JSON-serializable contract for reports and audit tools."""
    return {
        "version": METRIC_CONTRACT_VERSION,
        "composite_score": {
            "status": COMPOSITE_SCORE_STATUS,
            "formal_ready": False,
            "formal_gate": (
                "historical validation with sufficient dates and samples, "
                "leakage checks, stability review, and explicit operator approval"
            ),
            "weights": dict(_WEIGHTS),
        },
        "metrics": [asdict(item) for item in METRIC_DEFINITIONS],
    }


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _percentile(value: Any, values: Iterable[Any]) -> float | None:
    number = _number(value)
    population = sorted(
        item for item in (_number(candidate) for candidate in values)
        if item is not None
    )
    if number is None or not population:
        return None
    if len(population) == 1:
        return 100.0
    rank = sum(1 for item in population if item <= number)
    return max(0.0, min(100.0, rank * 100.0 / len(population)))


def _raw_value(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, "", "-"):
            return row[name]
    return None


def experimental_theme_score(
    row: Mapping[str, Any],
    peers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Score one theme using cross-sectional percentiles.

    ``peers`` must contain themes from the same trade date. Supplying no
    peers is allowed only when the row already contains ``*_pctile`` fields.
    Missing components produce no score instead of silently substituting zero.
    """
    peer_rows = list(peers) or [row]
    fields = {
        "breadth": ("breadth_pctile", ("limit_up_count", "breadth")),
        "height": ("height_pctile", ("max_board", "height", "highest_board")),
        "persistence": (
            "persistence_pctile",
            ("persistence_days", "duration_days", "persistence"),
        ),
        "flow": ("flow_pctile", ("main_net_inflow", "main_net", "flow")),
    }
    components: dict[str, float | None] = {}
    for component, (percentile_key, raw_keys) in fields.items():
        direct = _number(row.get(percentile_key))
        if direct is not None:
            components[component] = max(0.0, min(100.0, direct))
            continue
        value = _raw_value(row, raw_keys)
        components[component] = _percentile(
            value, (_raw_value(peer, raw_keys) for peer in peer_rows)
        )

    missing = [key for key, value in components.items() if value is None]
    result: dict[str, Any] = {
        "status": COMPOSITE_SCORE_STATUS,
        "formal_ready": False,
        "score": None,
        "components": components,
        "missing_components": missing,
        "contract_version": METRIC_CONTRACT_VERSION,
    }
    if missing:
        result["reason"] = "insufficient_components"
        return result
    result["score"] = round(
        sum(components[key] * _WEIGHTS[key] for key in _WEIGHTS), 4
    )
    result["reason"] = "experimental_only"
    return result


def _rank(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + end - 1) / 2.0 + 1.0
        for original, _ in ordered[index:end]:
            ranks[original] = rank
        index = end
    return ranks


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return numerator / (left_var * right_var) ** 0.5


def validate_experimental_theme_scores(
    samples: Sequence[Mapping[str, Any]],
    *,
    min_samples: int = 60,
    min_dates: int = 20,
) -> dict[str, Any]:
    """Summarize out-of-sample evidence without promoting the score.

    Each sample must contain ``trade_date``, ``score`` and
    ``forward_return_pct``. The function deliberately returns
    ``formal_ready=False`` even when sample thresholds are met; formalization
    requires a separate explicit operator decision after reviewing leakage,
    stability and economic significance.
    """
    usable = []
    for sample in samples:
        date = sample.get("trade_date") or sample.get("date")
        score = _number(sample.get("score"))
        forward = _number(sample.get("forward_return_pct", sample.get("forward_return")))
        if date and score is not None and forward is not None:
            usable.append((str(date), score, forward))
    scores = [item[1] for item in usable]
    returns = [item[2] for item in usable]
    unique_dates = len({item[0] for item in usable})
    enough = len(usable) >= min_samples and unique_dates >= min_dates
    ic = _correlation(_rank(scores), _rank(returns)) if enough else None
    result = {
        "contract_version": METRIC_CONTRACT_VERSION,
        "status": "validation_ready" if enough else "insufficient_sample",
        "formal_ready": False,
        "decision": "remain_experimental",
        "sample_count": len(usable),
        "date_count": unique_dates,
        "spearman_ic": round(ic, 6) if ic is not None else None,
        "positive_forward_rate": (
            round(sum(1 for value in returns if value > 0) / len(returns), 6)
            if returns else None
        ),
        "requirements": {
            "min_samples": min_samples,
            "min_dates": min_dates,
            "leakage_check": "required",
            "stability_check": "required",
            "operator_approval": "required",
        },
    }
    if not enough:
        result["reason"] = "not_enough_validated_history"
    return result

