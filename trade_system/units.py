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


def kpl_auction_tick_units(tick: dict) -> tuple[str, float | None, str]:
    """KPL GetStockFenBi2/StockL2History auction snapshots, not trade increments.

    2026-09-28 cross-check: 000710 21,563 hands = 2,156,300 shares;
    000850 308,513 hands = 30,851,300 shares. Both final prices and CNY
    amounts exactly match TuShare stk_auction (explicit shares/CNY contract).
    The retained 966 early snapshots also obey this scale. Some volumes are
    truncated to whole hands, so preserve source amount, not price*hands*100.
    Calibration receipts live in completion.json/auction_unit_calibration_20260928.
    Recheck the arithmetic on EVERY row; no magnitude-only or generic inference.
    """
    unit = str(tick.get('volume_unit') or tick.get('vol_unit') or 'unknown').lower()
    price, volume, amount = (_number(tick.get(k)) for k in ('price', 'volume', 'amount'))
    if price is None or price <= 0 or volume is None or volume < 0 or not volume.is_integer():
        return VOLUME_UNKNOWN, None, 'invalid_price_or_volume'
    if amount is None:
        if tick.get('amount') not in (None, '', '-'):
            return VOLUME_UNKNOWN, None, 'invalid_amount'
        return (unit, None, 'provider_declared') if unit in {'hands', 'shares'} else (VOLUME_UNKNOWN, None, 'amount_missing')
    if amount < 0:
        return VOLUME_UNKNOWN, None, 'invalid_amount'
    if unit not in {'unknown', 'hands', 'shares'}:
        return VOLUME_UNKNOWN, None, 'unsupported_declared_unit'
    if unit == 'unknown' and (tick.get('direction') not in {'集合竞价', '开盘撮合'} or
                              type(tick.get('flag')) is not int or tick['flag'] not in {0, 1, 2, 3}):
        return VOLUME_UNKNOWN, None, 'uncalibrated_payload_shape'
    factor = 1 if unit == 'shares' else 100
    residual = amount - price * volume * factor
    # Amount rounded to yuan; hands may discard up to 99 odd shares.
    limit = 1.01 if factor == 1 else price * 99 + 1.01
    if not -1.01 <= residual <= limit or (volume == 0 and amount != 0):
        return VOLUME_UNKNOWN, None, 'price_volume_amount_conflict'
    return ('hands' if unit == 'unknown' else unit), amount, (
        'kpl_auction_cross_source_20260928' if unit == 'unknown' else 'provider_declared_checked')


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
