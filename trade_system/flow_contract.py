"""Canonical capital-flow field contract.

The project historically called every provider's headline net amount
``main_net``.  That is unsafe: TuShare ``net_mf_amount`` is the total net
amount, while Eastmoney's ``PRIME_INFLOW`` is the main-order net amount.
This module keeps the raw payload untouched and normalizes the two meanings
into explicit fields.
"""

from __future__ import annotations

import json
import hashlib
from datetime import date
from typing import Any
from trade_system.units import _number as number, normalize_amount


FLOW_MAPPING_VERSION = "stock_flow_v3_explicit_units"
DEFAULT_AMOUNT_UNIT = "yuan"
# doc_id=170 declares SH/SZ A-share moneyflow; BJ field support is unproved.
# Expanding this set requires dated original product capability evidence.
TUSHARE_MONEYFLOW_EXCHANGES = frozenset({'SH', 'SZ'})

# Transport identity is not a new flow definition. Relay rows qualify only
# with their explicit native API, origin, units and main-order definition.
NATIVE_MAIN_FLOW_SQL = """(
    (provider IN ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay')
     OR (provider='xiaodefa_moneyflow_dc' AND origin_provider='eastmoney' AND source_api='moneyflow_dc'))
    AND amount_unit='yuan' AND isfinite(main_net)
    AND flow_definition IN ('provider_main_net','provider_main_orders_net','main_orders_net')
)"""

SECTOR_TAXONOMIES = {
    'em_industry': ('eastmoney.industry', 'provider_reported', 'main_orders_net'),
    'ths_industry': ('ths.industry', 'provider_reported', 'sector_total_net'),
    'ths_concept': ('ths.concept', 'provider_reported', 'declared_sector_flow'),
    'ths_concept_derived': ('ths.concept', 'member_aggregate', 'sum_member_main_orders_net'),
}


def normalize_sector_flow_row(row: dict, provider: str) -> dict:
    """Explicit taxonomy/measure identity; unknown types never become EM by prefix."""
    defaults = {'eastmoney':'em_industry', 'eastmoney_sector_full':'em_industry',
                'derived_ths_stock_aggregate':'ths_concept_derived'}
    raw_type = row.get('sector_type')
    kind = str(raw_type or defaults.get(provider) or 'unknown')
    contract = SECTOR_TAXONOMIES.get(kind)
    source_unit = row.get('amount_unit') or ('yuan' if provider in defaults else 'unknown')
    result = {field:normalize_amount(row.get(field), source_unit)
              for field in ('main_net','super_net','large_net','mid_net','small_net')}
    raw = row.get('raw') if isinstance(row.get('raw'), dict) else {}
    reason = 'unsupported_taxonomy' if not contract else None
    if reason is None and not any(value is not None for value in result.values()):
        reason = 'no_finite_declared_flow'
    result.update(sector_type=kind if contract else 'unknown', raw_sector_type=raw_type,
        taxonomy_namespace=contract[0] if contract else None,
        aggregation_kind=contract[1] if contract else None,
        flow_definition=contract[2] if contract else None,
        amount_unit='yuan', source_amount_unit=source_unit,
        mapping_version='sector-flow-contract-v1',
        quality_reason=reason,
        catalogue_version=row.get('catalogue_version') or raw.get('membership_snapshot_date'))
    return result


def _raw_dict(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") or row.get("raw_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def normalize_stock_flow_row(row: dict[str, Any], provider: str) -> dict[str, Any]:
    """Return canonical flow fields without discarding provider payload data."""
    # A canonical missing value is a decision, not permission to reinterpret raw units.
    already_normalized = (row.get("field_mapping_version") == FLOW_MAPPING_VERSION
                          and row.get("amount_unit") == "yuan")
    raw = {} if already_normalized else _raw_dict(row)
    source_api = str(
        row.get("source_api")
        or row.get("api_name")
        or raw.get("source_api")
        or raw.get("api_name")
        or raw.get("source")
        or provider
    )
    provider_key = str(provider or "unknown").lower()
    api = source_api.lower()
    ths = api == 'moneyflow_ths'
    tushare_moneyflow = api == 'moneyflow' or (
        api == provider_key and provider_key in {'tushare', 'tushare_relay'})
    declared = row.get('amount_unit')
    yuan_units = {'yuan', 'CNY', 'yuan_from_10000', 'yuan_from_100m_yuan'}
    raw_multiplier = 10000 if declared is None and (ths or tushare_moneyflow) else 1
    if declared in {'万元', '10000_yuan'}:
        raw_multiplier = 10000
    known_unit = declared in yuan_units or declared in {'万元', '10000_yuan'} or (
        declared is None and (ths or tushare_moneyflow))

    def first(*values):
        return next((v for v in values if number(v) is not None), None)

    small = number(row.get("small_net"))
    mid = number(row.get("mid_net"))
    large = number(first(row.get("large_net"), row.get("big_net")))
    super_net = number(row.get("super_net"))

    # Some raw TuShare-shaped rows reach the store directly.  Reconstruct the
    # order buckets here so the contract is enforced at the persistence edge.
    def net_pair(buy: str, sell: str) -> float | None:
        left, right = number(first(row.get(buy), raw.get(buy))), number(first(row.get(sell), raw.get(sell)))
        return left - right if left is not None and right is not None else None

    if tushare_moneyflow:
        small = small if small is not None else net_pair("buy_sm_amount", "sell_sm_amount")
        mid = mid if mid is not None else net_pair("buy_md_amount", "sell_md_amount")
        large = large if large is not None else net_pair("buy_lg_amount", "sell_lg_amount")
        super_net = super_net if super_net is not None else net_pair("buy_elg_amount", "sell_elg_amount")
    elif ths:
        large = large if large is not None else number(first(row.get('buy_lg_amount'), raw.get('buy_lg_amount')))
    if raw_multiplier != 1:
        # Raw TuShare order buckets are in 10,000 yuan.  Adapter-produced
        # canonical buckets declare yuan and bypass this conversion.
        small = small * 10000 if small is not None else None
        mid = mid * 10000 if mid is not None else None
        large = large * 10000 if large is not None else None
        super_net = super_net * 10000 if super_net is not None else None

    small, mid, large, super_net = map(number, (small, mid, large, super_net))

    explicit_total = number(row.get("net_total"))
    reported_total = explicit_total
    if reported_total is None:
        reported_total = number(row.get("net_mf_amount"))
    if reported_total is None:
        reported_total = number(raw.get("net_mf_amount"))

    # TuShare moneyflow amounts are in 10,000 yuan.  The provider adapters
    # should normally convert them first; this branch makes direct ingestion
    # safe as well.
    if tushare_moneyflow:
        # ``net_total`` emitted by the adapters is already in yuan.  Only raw
        # TuShare ``net_mf_amount`` (10,000 yuan) needs this conversion.
        if reported_total is not None:
            reported_total *= raw_multiplier
        buckets = [value for value in (small, mid, large, super_net) if value is not None]
        if buckets and large is not None and super_net is not None:
            main_net = large + super_net
            definition = "main_orders_net"
        else:
            # Do not silently call a total net amount "main_net".
            main_net = None
            definition = "total_net_only"
    elif ths:
        reported_total = number(first(row.get('net_total'), row.get('net_amount'), raw.get('net_amount')))
        reported_total = reported_total * raw_multiplier if reported_total is not None else None
        main_net = large
        definition = 'ths_large_orders_net'
    else:
        main_net = number(row.get("main_net"))
        if main_net is None:
            main_net = number(row.get("net_amount"))
        main_net = main_net * raw_multiplier if main_net is not None else None
        definition = row.get("flow_definition")

    origin_provider = str(row.get("origin_provider") or raw.get("_src") or provider or "unknown")
    identity = str(row.get('ts_code') or raw.get('ts_code') or '')
    if tushare_moneyflow and identity.endswith('.BJ') and 'BJ' not in TUSHARE_MONEYFLOW_EXCHANGES:
        main_net = reported_total = small = mid = large = super_net = None
        definition = 'product_security_scope_unverified'
    return {
        "main_net": number(main_net),
        "net_total": number(reported_total),
        "super_net": number(super_net),
        "large_net": number(large),
        "mid_net": number(mid),
        "small_net": number(small),
        "amount_unit": "yuan" if known_unit else str(declared or 'unknown'),
        "flow_unit": "CNY" if known_unit else None,
        "turnover_unit": "CNY" if row.get('turnover_unit') in yuan_units else None,
        "flow_definition": definition,
        "source_api": source_api,
        "origin_provider": origin_provider,
        "field_mapping_version": FLOW_MAPPING_VERSION,
    }


# Reviewed mappings only. Each key binds BOTH original origins, products,
# definitions and adapter versions. Provider rows cannot authorize a mapping.
# No current pair has documented equivalent order grouping/side/buckets/session.
# Values must include dated validity and hashes of both original specifications.
VERIFIED_FLOW_COMPARISONS = {}

# Acquisition vendor and original producer are distinct. This public SDK
# describes a transport/field contract, not an independent definition mapping.
GANGTISE_SOURCE_COMMIT = 'f244b4741df82077b5afb7d4714289555b401db9'
GANGTISE_FLOW_URL = 'https://openapi.gangtise.com/application/open-quote/fund-flow/daily'
GANGTISE_FLOW_FIELDS = tuple(
    f'{bucket}{suffix}' for bucket in ('small', 'medium', 'large', 'xlarge', 'total', 'main')
    for suffix in ('Inflow', 'Outflow', 'NetInflow'))


def candidate_flow_request(trade_date, securities):
    """One explicit historical session; no lookup, pagination or date fallback."""
    import re
    day = date.fromisoformat(trade_date)
    if day.isoformat() != trade_date:
        raise ValueError('explicit ISO trade date required')
    if not isinstance(securities, (list, tuple)) or not 1 <= len(securities) <= 10000:
        raise ValueError('explicit bounded securities required')
    if (any(not isinstance(c, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', c) for c in securities)
            or len(set(securities)) != len(securities)):
        raise ValueError('unique exchange-qualified securities required')
    return dict(securityList=list(securities), startDate=trade_date, endDate=trade_date,
                limit=len(securities), fieldList=list(GANGTISE_FLOW_FIELDS))


def candidate_flow_capabilities(settings=None):
    """Local configuration presence only; never authenticate or claim entitlement."""
    import os
    import importlib.util
    from trade_system.config import SETTINGS
    values = SETTINGS if settings is None else settings

    def present(key):
        value = values.get(key) or (os.environ.get(key) if settings is None else '')
        return bool(isinstance(value, str) and value.strip())

    return {
        'hithink_ai_client_key_present': present('HITHINK_FINANCE_API_KEY'),
        'ifind_access_token_present': present('IFIND_ACCESS_TOKEN'),
        'ifind_refresh_token_present': present('IFIND_REFRESH_TOKEN'),
        'ifind_sdk_present': importlib.util.find_spec('iFinDPy') is not None,
        'gangtise_authorization_present': present('GANGTISE_AUTHORIZATION'),
        'gangtise_ak_sk_present': present('GANGTISE_AK') and present('GANGTISE_SK'),
        'entitlement_verified': False,
        'hithink_ai_key_authorizes_ifind': False,
        'independent_definition_verified': False,
    }


def parse_candidate_flow_response(raw, trade_date, securities, *, received_at):
    """Audit original bytes without qualifying a provider or writing canonical facts."""
    from datetime import datetime
    from decimal import Decimal, InvalidOperation
    import math
    request = candidate_flow_request(trade_date, securities)
    arrival = datetime.fromisoformat(received_at)
    if arrival.utcoffset() is None:
        raise ValueError('original arrival must include timezone')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 8_000_000:
        raise ValueError('bounded original response bytes required')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate response object field')
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError('nonfinite JSON constant')

    body = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                      parse_constant=invalid_constant)
    if (not isinstance(body, dict) or body.get('code') != '000000'
            or body.get('status') is False):
        raise ValueError('provider business response rejected')
    block = body.get('data')
    if not isinstance(block, dict):
        raise ValueError('candidate response data missing')
    fields, records = block.get('fieldList'), block.get('list')
    if (not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)
            or len(set(fields)) != len(fields)
            or not {'securityCode', 'tradeDate', *GANGTISE_FLOW_FIELDS} <= set(fields)
            or not isinstance(records, list) or len(records) > request['limit']):
        raise ValueError('unknown candidate field or row contract')
    expected, seen, rows = set(securities), set(), []

    def amount(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError('invalid candidate amount')
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError('invalid candidate amount') from exc
        if not result.is_finite() or not math.isfinite(float(result)):
            raise ValueError('nonfinite candidate amount')
        return result

    for record in records:
        if not isinstance(record, list) or len(record) != len(fields):
            raise ValueError('candidate field/row width mismatch')
        item = dict(zip(fields, record))
        code, source_date = item['securityCode'], item['tradeDate']
        if (not isinstance(code, str) or code not in expected or code in seen):
            raise ValueError('unexpected or duplicate candidate identity')
        if source_date not in (trade_date, trade_date.replace('-', '')):
            raise ValueError('candidate response date differs from request')
        seen.add(code)
        numbers = {f: amount(item[f]) for f in GANGTISE_FLOW_FIELDS}
        issues = ['missing_amount:' + f for f, v in numbers.items() if v is None]
        for bucket in ('small', 'medium', 'large', 'xlarge', 'total', 'main'):
            buy, sell, net = (numbers[bucket + s] for s in ('Inflow', 'Outflow', 'NetInflow'))
            if (buy is not None and buy < 0) or (sell is not None and sell < 0):
                issues.append('negative_gross:' + bucket)
            if all(v is not None for v in (buy, sell, net)) and buy - sell != net:
                issues.append('net_arithmetic:' + bucket)
        for suffix in ('Inflow', 'Outflow', 'NetInflow'):
            values = [numbers[b + suffix] for b in ('large', 'xlarge', 'main')]
            if all(v is not None for v in values) and values[0] + values[1] != values[2]:
                issues.append('main_bucket_arithmetic:' + suffix)
            values = [numbers[b + suffix] for b in ('small', 'medium', 'large', 'xlarge', 'total')]
            if all(v is not None for v in values) and sum(values[:4]) != values[4]:
                issues.append('total_bucket_arithmetic:' + suffix)
        rows.append(dict(security_code=code, source_date=trade_date,
                         received_at=received_at, candidate_main_net_yuan=(
                             str(numbers['mainNetInflow']) if numbers['mainNetInflow'] is not None else None),
                         arithmetic_qualified=not issues, quality_issues=issues))
    missing = sorted(expected - seen)
    return dict(provider='gangtise_candidate', origin_provider='unknown',
                source_api='fund-flow/daily', source_commit=GANGTISE_SOURCE_COMMIT,
                request=request, request_sha256=hashlib.sha256(json.dumps(
                    request, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                response_sha256=hashlib.sha256(raw).hexdigest(), received_at=received_at,
                expected_rows=len(expected), returned_rows=len(rows), missing_securities=missing,
                rows=rows, returned_scope_complete=not missing,
                arithmetic_qualified=bool(rows) and all(r['arithmetic_qualified'] for r in rows),
                independent_comparison_eligible=False,
                missing_evidence=['original_producer', *FLOW_DEFINITION_AXES],
                production_writes=0, canonical_writes=0)


def request_candidate_flow(trade_date, securities, *, authorization, entitlement_sha256):
    """Explicit diagnostic consumer; caller supplies a separately approved budget.

    No credential login/refresh, retries, name lookup, fallback or production write.
    Receipt hashes bind the exact request and bytes; they do not certify entitlement.
    """
    import re
    from datetime import datetime, timezone
    from urllib.request import Request
    from trade_system.http_transport import diagnostic_state, read_verified_once, request_budget, stop_diagnostic
    if diagnostic_state.get() is None:
        raise RuntimeError('explicit newly authorized diagnostic context required')
    if (not isinstance(entitlement_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', entitlement_sha256)):
        raise ValueError('reviewed entitlement document hash required')
    if not isinstance(authorization, str) or not authorization.strip() or any(
            c in authorization for c in '\r\n'):
        raise ValueError('local authorization required')
    payload = candidate_flow_request(trade_date, securities)
    wire = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    token = authorization.strip()
    if not token.lower().startswith('bearer '):
        token = 'Bearer ' + token
    state = diagnostic_state.get()
    if state['stopped']:
        raise RuntimeError('diagnostic stopped: ' + state['stopped'])
    # Reuse complete valid bytes in this bounded diagnosis without refreshing
    # their arrival time or duplicating a successful request. Tokens stay local.
    from copy import deepcopy
    cache_key = (hashlib.sha256(wire).hexdigest(), hashlib.sha256(token.encode()).hexdigest(),
                 entitlement_sha256)
    cache = state.setdefault('candidate_flow_cache', {})
    if cache_key in cache:
        raw, result = cache[cache_key]
        return raw, dict(deepcopy(result), reused=True)
    request = Request(GANGTISE_FLOW_URL, data=wire, method='POST',
                      headers={'Authorization': token, 'Content-Type': 'application/json'})
    with request_budget(20):
        raw = read_verified_once(request, timeout=20, max_bytes=8_000_000)
    arrival = datetime.now(timezone.utc).isoformat()
    # Rejected business responses stop the shared diagnosis even with HTTP 200.
    try:
        result = parse_candidate_flow_response(raw, trade_date, securities, received_at=arrival)
    except (ValueError, UnicodeError):
        stop_diagnostic('business_rejected')
        # Return original bytes for isolated preservation even when rejected.
        # Provider messages are not echoed into diagnostics or terminal output.
        return raw, dict(status='response_rejected', request=payload,
                         response_sha256=hashlib.sha256(raw).hexdigest(), received_at=arrival,
                         independent_comparison_eligible=False, production_writes=0,
                         entitlement_document_sha256=entitlement_sha256)
    result['entitlement_document_sha256'] = entitlement_sha256
    result['reused'] = False
    if result['returned_scope_complete'] and result['arithmetic_qualified']:
        cache[cache_key] = (raw, deepcopy(result))
    return raw, result

# Product-specific, reviewed specifications only. Auction calibration does
# not authorize this money-flow product. Until a specification is accepted,
# retained points remain raw evidence and cannot contribute to a score.
VERIFIED_KPL_FLOW_PRODUCTS = {}


def ensure_kpl_flow_evidence(con):
    for name, kind in (('raw_json', 'VARCHAR'), ('raw_sha256', 'VARCHAR'),
                       ('sequence_id', 'VARCHAR'), ('amount_unit', 'VARCHAR'),
                       ('quantity_semantics', 'VARCHAR'), ('contract_sha256', 'VARCHAR'),
                       ('source_date_verified', 'BOOLEAN'), ('sequence_complete', 'BOOLEAN')):
        con.execute(f'ALTER TABLE advanced_zjmm_min ADD COLUMN IF NOT EXISTS {name} {kind}')


def kpl_flow_projection_sql(con):
    """One query shared by the source store and scoring view, fail closed.

    Select the newest acquisition before eligibility checks: a newer unknown
    or mixed response cannot silently revive an older qualified sequence.
    Missing buckets remain NULL; source arrival times never become build time.
    """
    empty = '''SELECT CAST(NULL AS DATE) AS date, CAST(NULL AS VARCHAR) AS stock_code,
        CAST(NULL AS DOUBLE) AS main_net, CAST(NULL AS DOUBLE) AS super_net,
        CAST(NULL AS DOUBLE) AS large_net, CAST(NULL AS TIMESTAMP) AS fetched_at,
        CAST(NULL AS TIMESTAMP) AS input_received_at_max, CAST(NULL AS TIMESTAMP) AS source_event_time,
        CAST(NULL AS BIGINT) AS point_count, CAST(NULL AS VARCHAR) AS flow_definition,
        CAST(NULL AS VARCHAR) AS quantity_semantics, CAST(NULL AS VARCHAR) AS raw_json WHERE FALSE'''
    columns = {r[1] for r in con.execute("PRAGMA table_info('advanced_zjmm_min')").fetchall()}
    required = {'raw_json', 'raw_sha256', 'sequence_id', 'amount_unit', 'quantity_semantics',
                'contract_sha256', 'source_date_verified', 'sequence_complete', 'time', 'fetched_at'}
    if not required <= columns or not VERIFIED_KPL_FLOW_PRODUCTS:
        return empty
    def literal(v):
        return "'" + str(v).replace("'", "''") + "'"
    specifications = []
    for digest, evidence in VERIFIED_KPL_FLOW_PRODUCTS.items():
        if (len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                or evidence.get('product') != '/advanced/zjmm-min'
                or evidence.get('unit') != 'yuan'
                or evidence.get('quantity_semantics') not in {'cumulative_snapshot', 'increment'}
                or not evidence.get('flow_definition')):
            continue
        try:
            start, end = (date.fromisoformat(evidence[k]) for k in ('valid_from', 'valid_through'))
        except (KeyError, ValueError, TypeError):
            continue
        specifications.append('(' + ','.join(literal(v) for v in
            (digest, str(start), str(end), evidence['quantity_semantics'], evidence['flow_definition'])) + ')')
    if not specifications:
        return empty
    def amount(col):
        return (f"CASE WHEN first(quantity_semantics)='cumulative_snapshot' THEN "
        f"first({col} ORDER BY try_cast(time AS TIME) DESC) ELSE "
        f"CASE WHEN count({col})=count(*) THEN sum({col}) END END")
    bound_amounts = ' AND '.join(f"try_cast(json_extract_string(raw_json,'$.parsed.{f}') AS BIGINT) "
        f"IS NOT DISTINCT FROM {f}" for f in ('main_net_inflow','super_net_inflow','big_net_inflow'))
    declared = lambda field: (f"coalesce(json_extract_string(raw_json,'$.item.{field}'),"
                              f"json_extract_string(raw_json,'$.envelope.{field}'))")
    source_day = "coalesce(" + ','.join(f"json_extract_string(raw_json,'$.{part}.{field}')"
        for part in ('item','envelope') for field in ('date','trade_date','day')) + ")"
    return f'''WITH specifications(digest,start_day,end_day,semantics,definition) AS
        (VALUES {','.join(specifications)}), ranked AS (
        SELECT *, dense_rank() OVER (PARTITION BY date,stock_code
            ORDER BY fetched_at DESC,sequence_id DESC) AS acquisition_rank
        FROM advanced_zjmm_min), latest AS (SELECT * FROM ranked WHERE acquisition_rank=1),
        qualified AS (SELECT p.*,s.definition FROM latest p LEFT JOIN specifications s
            ON p.contract_sha256=s.digest AND p.date BETWEEN CAST(s.start_day AS DATE) AND CAST(s.end_day AS DATE)
            AND p.quantity_semantics=s.semantics)
        SELECT date,stock_code,{amount('main_net_inflow')} AS main_net,
            {amount('super_net_inflow')} AS super_net,{amount('big_net_inflow')} AS large_net,
            min(fetched_at) AS fetched_at,max(fetched_at) AS input_received_at_max,
            date+max(try_cast(time AS TIME)) AS source_event_time,
            count(*) AS point_count,min(definition) AS flow_definition,
            min(quantity_semantics) AS quantity_semantics,
            to_json(list(raw_json ORDER BY try_cast(time AS TIME))) AS raw_json
        FROM qualified GROUP BY date,stock_code,sequence_id
        HAVING count(DISTINCT contract_sha256)=1 AND count(DISTINCT quantity_semantics)=1
            AND bool_and(coalesce(definition IS NOT NULL AND amount_unit='yuan'
                AND source_date_verified IS TRUE AND sequence_id IS NOT NULL
                AND raw_sha256=sha256(raw_json) AND try_cast(time AS TIME) IS NOT NULL
                AND json_extract_string(raw_json,'$.product')='/advanced/zjmm-min'
                AND json_extract_string(raw_json,'$.requested_code')=stock_code
                AND replace(json_extract_string(raw_json,'$.requested_date'),'-','')=strftime(date,'%Y%m%d')
                AND replace({source_day},'-','')=strftime(date,'%Y%m%d')
                AND {declared('amount_unit')}=amount_unit
                AND {declared('quantity_semantics')}=quantity_semantics
                AND {declared('contract_sha256')}=contract_sha256
                AND {bound_amounts}
                AND date+try_cast(time AS TIME)<=fetched_at,FALSE))
            AND count(DISTINCT try_cast(time AS TIME))=count(*)
            AND (first(quantity_semantics)='cumulative_snapshot' OR bool_and(sequence_complete IS TRUE
                AND try_cast(json_extract_string(raw_json,'$.envelope.sequence_complete') AS BOOLEAN) IS TRUE))'''

FLOW_DEFINITION_AXES = ('order_grouping', 'trade_side', 'size_buckets',
                        'session', 'security_scope', 'net_formula')


def stock_flow_evidence_fingerprint(con, trade_date, provider):
    """Invalidate persisted reconciliation when its values or provenance change."""
    rows = con.execute('''SELECT stock_code,main_net,origin_provider,source_api,
        flow_definition,amount_unit,field_mapping_version,fetched_at,raw_json
        FROM multi_source_stock_flow WHERE source_date=? AND provider=? AND is_stale=FALSE
        ORDER BY ALL''', [trade_date,provider]).fetchall()
    return hashlib.sha256(json.dumps(rows,default=str,separators=(',',':')).encode()).hexdigest()


def flow_definition_evidence(evidence, trade_date):
    """Validate reviewed specifications, not similarity of reported numbers."""
    missing = []
    try:
        day = date.fromisoformat(str(trade_date)[:10])
        start = date.fromisoformat(evidence['valid_from'])
        end = date.fromisoformat(evidence['valid_through'])
        if not start <= day <= end:
            missing.append('validity')
    except (KeyError, TypeError, ValueError):
        missing.append('validity')
    hashes = evidence.get('source_specification_sha256', [])
    if (not isinstance(hashes, list) or len(hashes) != 2
            or any(not isinstance(h, str) or len(h) != 64
                   or any(ch not in '0123456789abcdef' for ch in h) for h in hashes)):
        missing.append('source_specification_sha256')
    if not evidence.get('canonical_definition'):
        missing.append('canonical_definition')
    specifications = evidence.get('specifications', [])
    if not isinstance(specifications, list) or len(specifications) != 2:
        return sorted(set(missing + list(FLOW_DEFINITION_AXES)))
    for axis in FLOW_DEFINITION_AXES:
        values = [s.get(axis) if isinstance(s, dict) else None for s in specifications]
        if (not all(isinstance(v, str) and v.strip() and v.lower() not in {'unknown', 'unverified'}
                    for v in values) or values[0] != values[1]):
            missing.append(axis)
    return sorted(set(missing))


def independent_comparison_contract(con, trade_date, primary, reference):
    """Read-only semantic gate. Correlation cannot establish equivalent definitions.

    Different relays of one origin are never independent. Missing or mixed
    provenance and unlike provider definitions remain diagnostics only.
    """
    result = {'eligible': False, 'reason': 'metadata_unavailable', 'sides': {}}
    try:
        for label, provider in (('primary', primary), ('reference', reference)):
            groups = con.execute('''SELECT origin_provider,source_api,flow_definition,
                amount_unit,field_mapping_version,count(*),
                count(*) FILTER (WHERE isfinite(main_net))
                FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE)
                AND provider=? AND is_stale=FALSE GROUP BY ALL''', [trade_date, provider]).fetchall()
            if len(groups) != 1:
                result['reason'] = 'missing_or_mixed_provenance'
                return result
            origin, api, definition, unit, version, count, finite = groups[0]
            result['sides'][label] = dict(provider=provider, origin=origin, api=api,
                definition=definition, unit=unit, mapping_version=version, rows=count)
            if (not all((origin, api, definition, version)) or unit != 'yuan'
                    or origin in {'unknown', 'xiaodefa', 'tushare_relay'} or count != finite):
                result['reason'] = 'unqualified_provenance_or_unit'
                return result
    except Exception:
        return result
    left, right = result['sides']['primary'], result['sides']['reference']
    if left['origin'] == right['origin']:
        result['reason'] = 'same_original_source'
    else:
        signatures = tuple(sorted(tuple(side[k] for k in
            ('origin', 'api', 'definition', 'mapping_version')) for side in (left, right)))
        evidence = VERIFIED_FLOW_COMPARISONS.get(signatures, {})
        missing = flow_definition_evidence(evidence, trade_date)
        if missing:
            result['reason'] = ('definition_alignment_unproven' if left['definition'] != right['definition']
                                else 'definition_evidence_missing')
            result['missing_definition_evidence'] = missing
        else:
            result.update(eligible=True, reason='verified_independent_definition_mapping',
                          definition_evidence=evidence)
    return result


def ensure_stock_flow_contract(con) -> None:
    """Upgrade old DuckDB files in place; no rows are removed."""
    existing = {
        row[1]
        for row in con.execute(
            "PRAGMA table_info('multi_source_stock_flow')"
        ).fetchall()
    }
    for column, kind in (
        ("net_total", "DOUBLE"),
        ("amount_unit", "VARCHAR"),
        ("flow_unit", "VARCHAR"),
        ("turnover_unit", "VARCHAR"),
        ("flow_definition", "VARCHAR"),
        ("source_api", "VARCHAR"),
        ("origin_provider", "VARCHAR"),
        ("field_mapping_version", "VARCHAR"),
    ):
        if column not in existing:
            con.execute(
                f"ALTER TABLE multi_source_stock_flow ADD COLUMN {column} {kind}"
            )
