"""Pure per-security capability projection from one freshly validated assessment.

This module does not read cached green flags, acquire inputs, or write facts.
The retained reader remains responsible for receipt/original revalidation and
for supplying the same observation cutoff to its committed-fact queries.
"""
from datetime import datetime
import math
from zoneinfo import ZoneInfo

from trade_system.units import _number as _num


def capability_projection(report, expected, committed, review_receipts, check_deadline):
    zone = ZoneInfo('Asia/Shanghai')
    at = datetime.fromisoformat(report['observed_at'])
    day = report['trade_date']
    historical = day < at.astimezone(zone).date().isoformat()
    rows, qualified_reviews = {}, {}
    for code, item in report['rows'].items():
        check_deadline()
        scope_ok = expected is not None and code in expected
        reviewed = (bool(item.get('valuation_eligible')) and scope_ok
                    and item.get('review_sha256') in review_receipts)
        if reviewed:
            qualified_reviews[code] = item
        native = item['native_valuation']
        original = native['values']
        canonical = committed.get(code)
        native_matches = False
        if canonical and native['receipt_sha256']:
            fetched = canonical[5]
            if isinstance(fetched, datetime):
                fetched = fetched.replace(tzinfo=zone) if fetched.tzinfo is None else fetched
                native_matches = fetched <= at
        fields = {}
        for index, field in enumerate(('pb', 'total_mv', 'circ_mv', 'pe'), 1):
            raw_value = _num(original.get(field))
            numeric = raw_value is not None and math.isfinite(raw_value)
            if field in {'total_mv', 'circ_mv'}:
                numeric = numeric and raw_value > 0
            matched = (scope_ok and native_matches and numeric
                       and _num(canonical[index]) == raw_value)
            if field == 'circ_mv':
                total = _num(original.get('total_mv'))
                matched = matched and total is not None and math.isfinite(total) and raw_value <= total
            value = item.get('values', {}).get(field) if reviewed else None
            review_status = item.get('field_status', {}).get(field, 'unknown')
            review_numeric = value is not None and math.isfinite(value)
            qualified = (reviewed and review_numeric) or matched
            known_undefined = reviewed and review_status in {
                'undefined_zero_adjusted_equity', 'not_applicable_nonpositive_ordinary_profit'}
            fields[field] = {
                'status': (review_status if reviewed and review_numeric else
                           'qualified_native_field' if matched else
                           review_status if known_undefined else 'unknown'),
                'value': value if reviewed and review_numeric else raw_value if matched else None,
                'numeric_qualified_for_session': bool(qualified),
                'numeric_available_currently': bool(qualified and not historical),
                'known_undefined': bool(known_undefined),
                'receipt_sha256': (item.get('review_sha256') if reviewed else
                                   native['receipt_sha256'] if matched else None),
            }
        ttm = item.get('values', {}).get('pe_ttm') if reviewed else None
        ttm_qualified = ttm is not None and math.isfinite(ttm)
        ttm_status = item.get('field_status', {}).get('pe_ttm', 'unknown') if reviewed else 'unknown'
        fields['pe_ttm'] = {'status': ttm_status,
            'value': ttm if ttm_qualified else None, 'numeric_qualified_for_session': ttm_qualified,
            'numeric_available_currently': bool(ttm_qualified and not historical),
            'known_undefined': ttm_status == 'not_applicable_nonpositive_ordinary_profit'}
        pb = fields['pb']
        native_core = all(fields[f]['numeric_qualified_for_session'] for f in ('pb', 'total_mv', 'circ_mv'))
        reasons = {} if reviewed or native_core else dict(item.get('missing_inputs') or {})
        if not scope_ok:
            reasons['identity_scope'] = ['expected_universe_unverified' if expected is None else 'outside_expected_universe']
        if not pb['numeric_qualified_for_session'] and not pb.get('known_undefined'):
            reasons.setdefault('pb', ['no_qualified_same_session_pb'])
        if item.get('valuation_eligible') and scope_ok and not reviewed:
            reasons['review'] = ['review_not_registered_at_observation']
        if item.get('review_revalidation_error'):
            reasons['review'] = [item['review_revalidation_error']]
        earnings_missing = dict(item.get('earnings_missing_inputs') or {})
        for field in ('pe', 'pe_ttm'):
            if not fields[field]['numeric_qualified_for_session'] and not fields[field]['known_undefined']:
                earnings_missing.setdefault(field, ['qualified_profit_or_same_session_field_missing'])
        rows[code] = {
            'trade_date': day, 'historical_as_of': historical,
            'included_in_expected_universe': None if expected is None else code in expected,
            'qualified_core_for_session': bool(reviewed or native_core), 'fields': fields,
            'current_core_available': bool((reviewed or native_core) and not historical),
            'value_screen_eligible_for_session': bool(pb['numeric_qualified_for_session'] and pb['value'] > 0),
            'value_screen_eligible': bool(not historical and pb['numeric_qualified_for_session'] and pb['value'] > 0),
            'reasons': reasons, 'earnings_missing_inputs': earnings_missing,
            'financial_reference': item.get('financial_reference'),
            'native_pb_status': item.get('native_pb_status'),
            'input_received_at_min': item.get('input_received_at_min'),
            'input_received_at_max': item.get('input_received_at_max'),
            'native_received_at': native['received_at'],
            'review_revalidation_error': item.get('review_revalidation_error'),
            'does_not_assess': ['quote', 'trade', 'primary_flow', 'execution'],
        }
    failed = any(row['review_revalidation_error'] for row in rows.values())
    status = ('partial' if any(row['qualified_core_for_session'] for row in rows.values()) else 'unknown') if failed else 'assessed'
    return {'schema': 'per_security_valuation_capabilities_v1', 'trade_date': day, 'status': status,
        'observed_at': at.isoformat(), 'historical_as_of': historical, 'rows': rows,
        'reviewed_valuation': qualified_reviews,
        'scope': 'valuation_only', 'market_requests': 0, 'business_rows_written': 0,
        'certifies_daily_basic': False, 'changes_expected_universe': False,
        'unattended_all_market_valuation_promise': 'retired',
        'daily_review_requirement': 'same_session_inputs_named_review_exact_sha_receipt_intake'}
