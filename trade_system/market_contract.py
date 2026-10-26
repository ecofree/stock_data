"""Shared qualification for the five daily market counts, separate from freshness.

Available means all five counts are finite nonnegative integers and the source
does not declare partial/estimated data. Zero is a known observation, not a
replacement for NULL. Scores have additional, independently required inputs.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json


def market_count(value):
    """A missing/bool/fractional/overflowing observation is never a zero count."""
    if value is None or isinstance(value, bool):
        raise ValueError('market count must be an explicit nonnegative integer')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('invalid market count') from exc
    if not number.is_finite() or number != number.to_integral_value() or not 0 <= number <= 2147483647:
        raise ValueError('invalid market count')
    return int(number)


def validate_requested_day(data, requested):
    """Check explicit dates; absent dates are request-bound, not source timestamps."""
    expected = date.fromisoformat(str(requested))
    if isinstance(data, dict):
        for key in ('date', 'trade_date', 'source_date'):
            if key in data:
                value = data[key]
                if not isinstance(value, str):
                    raise ValueError('invalid response date')
                if date.fromisoformat(value) != expected:
                    raise ValueError('response date does not match request')


def require_observed_counts(data):
    if (data.get('is_fallback') or ('source_kind' in data and data['source_kind'] != 'real')
            or ('market_daily_status' in data and data['market_daily_status'] != 'available')):
        raise ValueError('unqualified market source')


def count_evidence(value, *, source, source_kind, source_date, fetched_at, received_at):
    """Bounded constituent evidence; never copy a previous raw audit payload.

    fetched_at is the conservative receipt clock of the source relation, not
    a claimed upstream observation time. Unknown/future clocks fail closed.
    """
    try:
        parsed = market_count(value)
    except ValueError:
        parsed = None
    clock = fetched_at if isinstance(fetched_at, datetime) and fetched_at.tzinfo is None else None
    qualified = (parsed is not None and source_kind == 'real' and clock is not None
                 and clock.tzinfo is None and clock <= received_at)
    return {'value': parsed, 'source': source, 'source_kind': source_kind,
            'source_date': str(source_date), 'source_effective_at': None,
            'freshness_basis': 'source_relation_receipt_lower_bound',
            'fetched_at': clock.isoformat(timespec='microseconds') if clock else None,
            'qualified': qualified}


def inherited_count_evidence(value, field, *, provenance, source_kind, source_date, fetched_at, received_at):
    """Use bounded scalar lineage, never the potentially huge legacy raw_json."""
    if provenance is not None:
        try:
            if len(provenance) > 8192:
                raise ValueError('oversized provenance')
            entry = json.loads(provenance)[field]
            if entry['source'] not in ('kpl_daily','kpl_market_rise_fall','market_mood','daily_summary'):
                raise ValueError('unknown count source')
            if market_count(entry['value']) != market_count(value) or entry['source_date'] != str(source_date):
                raise ValueError('provenance does not bind this observation')
            # Re-evaluate all facts, including a clock that may be future as-of.
            clock = datetime.fromisoformat(entry['fetched_at']) if entry['fetched_at'] else None
            return count_evidence(value, source=entry['source'], source_kind=entry['source_kind'],
                source_date=source_date, fetched_at=clock, received_at=received_at)
        except (TypeError, KeyError, ValueError):
            # A malformed explicit receipt must not fall back to the row label.
            source_kind = 'unverified'
    return count_evidence(value, source='daily_summary', source_kind=source_kind,
        source_date=source_date, fetched_at=fetched_at, received_at=received_at)

MARKET_COUNT_FIELDS = (
    'limit_up_count', 'limit_down_count', 'rise_count', 'fall_count', 'consecutive_count',
)
MARKET_STATE_RELATIONS = ('v_market_state_inputs', 'v_market_daily', 'daily_summary')


def count_valid_sql(expression: str) -> str:
    value = f'TRY_CAST({expression} AS DOUBLE)'
    return f'coalesce(isfinite({value}) AND {value} >= 0 AND {value} = floor({value}), false)'


def market_daily_status_sql(columns, prefix='') -> str:
    """SQL used by normalization and all market-state candidate readers."""
    columns = set(columns)
    valid = ' AND '.join(count_valid_sql(prefix + name) if name in columns else 'false'
                         for name in MARKET_COUNT_FIELDS)
    partial = (f"lower(coalesce({prefix}source_kind,'')) LIKE '%partial%'"
               if 'source_kind' in columns else 'false')
    fallback = (f"lower(coalesce({prefix}source_kind,'')) IN ('fallback','derived','derived_current')"
                if 'source_kind' in columns else 'false')
    if 'is_fallback' in columns:
        fallback += f' OR coalesce({prefix}is_fallback,false)'
    declared = ''
    if 'market_daily_status' in columns:
        declared = (f"WHEN coalesce({prefix}market_daily_status,'unverified') <> 'available' "
                    f"THEN coalesce({prefix}market_daily_status,'unverified') ")
    return (f"CASE WHEN NOT ({valid}) OR ({partial}) THEN 'partial' "
            f"{declared}WHEN ({fallback}) THEN 'estimated' ELSE 'available' END")
