"""Canonical market-data units used at source boundaries.

The project has several providers whose field names are identical while the
units are not.  Keep the vocabulary small and explicit so adapters can carry
their raw unit without making the downstream view guess.
"""

from __future__ import annotations

from typing import Any
import math


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

# Unit aliases, not provider-field semantics. Never infer units from magnitude.
UNIT_FACTORS = {
    'amount': {'yuan': 1.0, 'cny': 1.0, 'thousand_yuan': 1000.0,
               '10000_yuan': 10000.0, '万元': 10000.0, '100m_yuan': 100000000.0},
    'volume': {'shares': 1.0, 'hands': 100.0},
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, "", "-"):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def conversion(value: Any, unit: str | None, kind: str) -> dict:
    """Finite canonical value and a reason; raw values belong to immutable receipts."""
    factors = UNIT_FACTORS[kind]
    normalized = str(unit or 'unknown').strip().lower()
    number = _number(value)
    reason = 'source_missing' if value is None or value in ('', '-') else None
    if reason is None and number is None:
        reason = 'invalid_number'
    if reason is None and normalized not in factors:
        reason = 'source_not_provided' if normalized == 'not_provided' else 'unknown_unit'
    result = number * factors[normalized] if reason is None else None
    if result is not None and not math.isfinite(result):
        result, reason = None, 'numeric_overflow'
    return {'value': result, 'unit': 'yuan' if kind == 'amount' else 'shares',
            'source_unit': normalized, 'reason': reason}


def normalization_sql(value_sql: str, unit_sql: str, kind: str) -> str:
    """DuckDB expression generated from the same vocabulary (trusted SQL only)."""
    branches = ' '.join(f"WHEN '{unit}' THEN {factor}" for unit, factor in UNIT_FACTORS[kind].items())
    product = (f"(TRY_CAST({value_sql} AS DOUBLE) * CASE lower(trim(coalesce({unit_sql}, 'unknown'))) "
               f"{branches} ELSE NULL END)")
    return f"CASE WHEN typeof({value_sql}) <> 'BOOLEAN' AND isfinite({product}) THEN {product} ELSE NULL END"


def normalize_volume(value: Any, unit: str | None) -> float | None:
    """Return shares, or ``None`` when the source unit is not declared."""
    return conversion(value, unit, 'volume')['value']


def normalize_amount(value: Any, unit: str | None) -> float | None:
    """Return yuan, or ``None`` when the source unit is not declared."""
    return conversion(value, unit, 'amount')['value']


def market_caps(row: dict) -> dict:
    """Never merge float with total, or infer the legacy ambiguous billion label."""
    result, quality = {}, {}
    for source, target in [('total_mv', 'total_market_cap_cny'),
                           ('circ_mv', 'float_market_cap_cny')]:
        normalized = conversion(row.get(source), row.get(source+'_unit'), 'amount')
        value, reason = normalized['value'], normalized['reason']
        if value is not None and value < 0:
            value, reason = None, 'negative_market_cap'
        result[target], quality[target] = value, reason
    return dict(result, market_cap_quality=quality)


def requested_adjustment(fq: str | None) -> str:
    value = str(fq or "").strip().lower()
    return {
        "qfq": ADJUSTMENT_QFQ,
        "hfq": ADJUSTMENT_HFQ,
        "": ADJUSTMENT_NONE,
        "bfq": ADJUSTMENT_NONE,
    }.get(value, ADJUSTMENT_UNKNOWN)

def quote_source_event_time(data,provider):
    """Normalize an explicit supplier event, never a locally inferred date/time.

    Tencent's existing adapter exposes field 30 as a Shanghai YYYYMMDDhhmmss.
    Other adapters must supply an ISO timestamp with its explicit UTC offset.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import re
    value=data.get('source_event_time')
    try:
        if value:
            moment=datetime.fromisoformat(str(value))
            if moment.tzinfo is None:return None
            return moment.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        if provider=='tencent' and re.fullmatch(r'[0-9]{14}',str(data.get('time',''))):
            return datetime.strptime(data['time'],'%Y%m%d%H%M%S')
    except (ValueError,TypeError):
        return None
    return None
