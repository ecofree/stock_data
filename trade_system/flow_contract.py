"""Canonical capital-flow field contract.

The project historically called every provider's headline net amount
``main_net``.  That is unsafe: TuShare ``net_mf_amount`` is the total net
amount, while Eastmoney's ``PRIME_INFLOW`` is the main-order net amount.
This module keeps the raw payload untouched and normalizes the two meanings
into explicit fields.
"""

from __future__ import annotations

import json
from typing import Any
from trade_system.units import _number as number, normalize_amount


FLOW_MAPPING_VERSION = "stock_flow_v3_explicit_units"
DEFAULT_AMOUNT_UNIT = "yuan"

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
    raw = _raw_dict(row)
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
