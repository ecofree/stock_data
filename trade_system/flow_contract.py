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
