"""Shared qualification for the five daily market counts, separate from freshness.

Available means five finite nonnegative integer counts with matching dated,
observed constituent evidence. A row label or an earlier projection's status
cannot replace that evidence. Zero is an observation, not a replacement for
NULL. Scores have additional, independently required inputs.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from zoneinfo import ZoneInfo

COUNT_EVIDENCE_SOURCES = ('kpl_daily', 'kpl_market_rise_fall', 'market_mood', 'daily_summary')
MARKET_OBSERVATION_KINDS = ('real', 'real_cross_source', 'secondary_verified')
NAIVE_RECEIPT_CLOCK_PATTERN = '[0-9]{4}-[0-9]{2}-[0-9]{2}[T ]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]{1,6})?'


def _naive_receipt_clock(value):
    """Match the SQL receipt format before parsing; ISO parsing alone is broader."""
    if isinstance(value, datetime):
        return value if value.tzinfo is None else None
    if not isinstance(value, str) or re.fullmatch(NAIVE_RECEIPT_CLOCK_PATTERN, value) is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
    try:
        source_day = date.fromisoformat(str(source_date))
    except (ValueError, TypeError):
        source_day = None
    qualified = (parsed is not None and source in COUNT_EVIDENCE_SOURCES and source_kind == 'real'
                 and clock is not None and isinstance(received_at, datetime) and received_at.tzinfo is None
                 and source_day is not None and clock.date() >= source_day and clock <= received_at)
    return {'value': parsed, 'source': source, 'source_kind': source_kind,
            'source_date': str(source_date), 'source_effective_at': None,
            'freshness_basis': 'source_relation_receipt_lower_bound',
            'fetched_at': clock.isoformat(timespec='microseconds') if clock else None,
            'qualified': qualified}


def _unique_provenance_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('ambiguous duplicate provenance key')
        result[key] = value
    return result


def inherited_count_evidence(value, field, *, provenance, source_kind, source_date, fetched_at, received_at):
    """Use bounded scalar lineage, never the potentially huge legacy raw_json."""
    if provenance is not None:
        try:
            if source_kind not in (*MARKET_OBSERVATION_KINDS, 'real_partial'):
                raise ValueError('unknown stored observation identity')
            if len(provenance) > 8192:
                raise ValueError('oversized provenance')
            entry = json.loads(provenance, object_pairs_hook=_unique_provenance_object)[field]
            if entry['source'] not in COUNT_EVIDENCE_SOURCES:
                raise ValueError('unknown count source')
            if market_count(entry['value']) != market_count(value) or entry['source_date'] != str(source_date):
                raise ValueError('provenance does not bind this observation')
            if entry['qualified'] is not True or entry['freshness_basis'] != 'source_relation_receipt_lower_bound':
                raise ValueError('unqualified stored constituent')
            # Reuse the read-side time contract before canonicalizing a receipt.
            # Otherwise permissive ISO parsing launders date-only/minute clocks,
            # or a refreshed/missing aggregate clock, into newly valid evidence.
            clock = _naive_receipt_clock(entry['fetched_at'])
            row_clock = _naive_receipt_clock(fetched_at)
            source_day = date.fromisoformat(str(source_date))
            if (clock is None or row_clock is None or row_clock.date() < source_day
                    or row_clock > clock):
                raise ValueError('stored receipt clocks do not bind this observation')
            return count_evidence(value, source=entry['source'], source_kind=entry['source_kind'],
                source_date=source_date, fetched_at=clock, received_at=received_at)
        except (TypeError, KeyError, ValueError):
            pass
    # Missing and malformed stored receipts both remain unverified. Otherwise
    # the next successful rise/fall refresh could launder a historical row's
    # 'real' label into newly fabricated five-field provenance.
    return count_evidence(value, source='daily_summary', source_kind='unverified',
        source_date=source_date, fetched_at=fetched_at, received_at=received_at)

MARKET_COUNT_FIELDS = (
    'limit_up_count', 'limit_down_count', 'rise_count', 'fall_count', 'consecutive_count',
)
MARKET_STATE_RELATIONS = ('v_market_state_inputs', 'v_market_daily', 'daily_summary')


def count_valid_sql(expression: str) -> str:
    value = f'TRY_CAST({expression} AS DOUBLE)'
    return f'coalesce(isfinite({value}) AND {value} BETWEEN 0 AND 2147483647 AND {value} = floor({value}), false)'


def _count_provenance_valid_sql(columns, prefix, as_of):
    """Re-evaluate bounded scalar evidence, including its original receipt clock.

    daily_summary.fetched_at is a conservative lower bound for *every* count.
    A recent aggregate timestamp must not conceal an older constituent. The
    clock is checked again at each consumer's as-of, not only at collection.
    """
    day_column = next((n for n in ('trade_date', 'date', 'source_date') if n in columns), None)
    clock_column = next((n for n in ('fetched_at', 'updated_at', 'created_at') if n in columns), None)
    if not {'source_kind', 'count_provenance_json'} <= columns or not day_column or not clock_column:
        return 'false'
    if as_of is None:
        reference = "timezone('Asia/Shanghai', current_timestamp)"
    else:
        if not isinstance(as_of, datetime):
            raise TypeError('market qualification as_of must be a datetime')
        if as_of.tzinfo is not None:
            as_of = as_of.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        reference = "TIMESTAMP '" + as_of.isoformat(sep=' ', timespec='microseconds') + "'"
    raw = prefix + 'count_provenance_json'
    proof = f"CASE WHEN length({raw}) <= 8192 AND json_valid({raw}) THEN {raw} ELSE '{{}}' END"
    day = f'TRY_CAST({prefix}{day_column} AS DATE)'
    row_clock = f'TRY_CAST({prefix}{clock_column} AS TIMESTAMP)'
    naive_clock_pattern = NAIVE_RECEIPT_CLOCK_PATTERN
    source_kinds = ','.join(repr(value) for value in MARKET_OBSERVATION_KINDS)
    sources = ','.join(repr(value) for value in COUNT_EVIDENCE_SOURCES)
    conditions = [
        # Historical aliases alone do not certify anything: they need the same
        # five explicit receipts as current 'real' rows.
        f'{prefix}source_kind IN ({source_kinds})',
        f'json_type({proof}) = \'OBJECT\'',
        f'len(json_keys({proof})) = len(list_distinct(json_keys({proof})))',
        f'{day} IS NOT NULL',
        f"regexp_full_match(CAST({prefix}{clock_column} AS VARCHAR), '{naive_clock_pattern}')",
        f'isfinite({row_clock})',
        f'{row_clock} <= {reference}',
        f'CAST({row_clock} AS DATE) >= {day}',
    ]
    for field in MARKET_COUNT_FIELDS:
        get = lambda key: f"json_extract_string({proof}, '$.{field}.{key}')"
        value = get('value')
        clock_text = get('fetched_at')
        clock = f'TRY_CAST({clock_text} AS TIMESTAMP)'
        keys = f"json_keys({proof}, '$.{field}')"
        conditions.extend((
            f"json_type({proof}, '$.{field}') = 'OBJECT'",
            f'len({keys}) = len(list_distinct({keys}))',
            count_valid_sql(value),
            f'TRY_CAST({value} AS DOUBLE) = TRY_CAST({prefix}{field} AS DOUBLE)',
            f"{get('source')} IN ({sources})",
            f"{get('source_kind')} = 'real'",
            f"{get('source_date')} = CAST({day} AS VARCHAR)",
            f"json_type({proof}, '$.{field}.qualified') = 'BOOLEAN' AND {get('qualified')} = 'true'",
            f"{get('freshness_basis')} = 'source_relation_receipt_lower_bound'",
            # Collector receipts are local naive ISO timestamps. Reject offsets
            # rather than silently dropping their timezone during TIMESTAMP cast.
            f"regexp_full_match({clock_text}, '{naive_clock_pattern}')",
            f'isfinite({clock})',
            f'{clock} <= {reference}',
            f'CAST({clock} AS DATE) >= {day}',
            f'{row_clock} <= {clock}',
        ))
    return 'coalesce((' + ' AND '.join(conditions) + '), false)'


def market_daily_status_sql(columns, prefix='', *, as_of=None) -> str:
    """Shared read-side qualification; projected labels never replace receipts."""
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
    evidence = _count_provenance_valid_sql(columns, prefix, as_of) if set(MARKET_COUNT_FIELDS) <= columns else 'false'
    return (f"CASE WHEN NOT ({valid}) OR ({partial}) THEN 'partial' "
            f"{declared}WHEN ({fallback}) THEN 'estimated' "
            f"WHEN NOT ({evidence}) THEN 'unverified' ELSE 'available' END")
