"""Canonical market-data units used at source boundaries.

The project has several providers whose field names are identical while the
units are not.  Keep the vocabulary small and explicit so adapters can carry
their raw unit without making the downstream view guess.
"""

from __future__ import annotations

from typing import Any


VOLUME_SHARES = "shares"
VOLUME_HANDS = "hands"
VOLUME_UNKNOWN = "unknown"

AMOUNT_YUAN = "yuan"
AMOUNT_THOUSAND_YUAN = "thousand_yuan"
AMOUNT_UNKNOWN = "unknown"

ADJUSTMENT_NONE = "none"
ADJUSTMENT_QFQ = "qfq"
ADJUSTMENT_HFQ = "hfq"
ADJUSTMENT_UNKNOWN = "unknown"


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_volume(value: Any, unit: str | None) -> float | None:
    """Return shares, or ``None`` when the source unit is not declared."""
    number = _number(value)
    normalized = str(unit or VOLUME_UNKNOWN).strip().lower()
    if number is None:
        return None
    if normalized == VOLUME_SHARES:
        return number
    if normalized == VOLUME_HANDS:
        return number * 100.0
    return None


def normalize_amount(value: Any, unit: str | None) -> float | None:
    """Return yuan, or ``None`` when the source unit is not declared."""
    number = _number(value)
    normalized = str(unit or AMOUNT_UNKNOWN).strip().lower()
    if number is None:
        return None
    if normalized == AMOUNT_YUAN:
        return number
    if normalized == AMOUNT_THOUSAND_YUAN:
        return number * 1000.0
    return None


def requested_adjustment(fq: str | None) -> str:
    value = str(fq or "").strip().lower()
    return {
        "qfq": ADJUSTMENT_QFQ,
        "hfq": ADJUSTMENT_HFQ,
        "": ADJUSTMENT_NONE,
        "bfq": ADJUSTMENT_NONE,
    }.get(value, ADJUSTMENT_UNKNOWN)

