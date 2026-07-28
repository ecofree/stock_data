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


FLOW_MAPPING_VERSION = "stock_flow_v2"
DEFAULT_AMOUNT_UNIT = "yuan"


def number(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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
    tushare_moneyflow = provider_key in {"tushare", "tushare_relay"} or "moneyflow" in source_api.lower()

    small = number(row.get("small_net"))
    mid = number(row.get("mid_net"))
    large = number(row.get("large_net") or row.get("big_net"))
    super_net = number(row.get("super_net"))

    # Some raw TuShare-shaped rows reach the store directly.  Reconstruct the
    # order buckets here so the contract is enforced at the persistence edge.
    def net_pair(buy: str, sell: str) -> float | None:
        left, right = number(row.get(buy) or raw.get(buy)), number(row.get(sell) or raw.get(sell))
        return left - right if left is not None and right is not None else None

    small = small if small is not None else net_pair("buy_sm_amount", "sell_sm_amount")
    mid = mid if mid is not None else net_pair("buy_md_amount", "sell_md_amount")
    large = large if large is not None else net_pair("buy_lg_amount", "sell_lg_amount")
    super_net = super_net if super_net is not None else net_pair("buy_elg_amount", "sell_elg_amount")
    if tushare_moneyflow and row.get("amount_unit") not in {"yuan", "yuan_from_10000"}:
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
        if explicit_total is None and reported_total is not None and row.get("amount_unit") not in {"yuan", "yuan_from_10000"}:
            reported_total *= 10000.0
        buckets = [value for value in (small, mid, large, super_net) if value is not None]
        if buckets and any(value is not None for value in (large, super_net)):
            main_net = (large or 0.0) + (super_net or 0.0)
            definition = "main_orders_net"
        else:
            # Do not silently call a total net amount "main_net".
            main_net = None
            definition = "total_net_only"
    else:
        main_net = number(row.get("main_net"))
        if main_net is None:
            main_net = number(row.get("net_amount"))
        definition = str(row.get("flow_definition") or "provider_main_net")

    origin_provider = str(row.get("origin_provider") or raw.get("_src") or provider or "unknown")
    return {
        "main_net": main_net,
        "net_total": reported_total,
        "super_net": super_net,
        "large_net": large,
        "mid_net": mid,
        "small_net": small,
        "amount_unit": str(row.get("amount_unit") or "yuan"),
        "flow_definition": definition,
        "source_api": source_api,
        "origin_provider": origin_provider,
        "field_mapping_version": FLOW_MAPPING_VERSION,
    }


def ensure_stock_flow_contract(con) -> None:
    """Upgrade old DuckDB files in place; no rows are removed."""
    for column, kind in (
        ("net_total", "DOUBLE"),
        ("amount_unit", "VARCHAR"),
        ("flow_definition", "VARCHAR"),
        ("source_api", "VARCHAR"),
        ("origin_provider", "VARCHAR"),
        ("field_mapping_version", "VARCHAR"),
    ):
        con.execute(f"ALTER TABLE multi_source_stock_flow ADD COLUMN IF NOT EXISTS {column} {kind}")


def migrate_existing_stock_flow(con) -> dict[str, int]:
    """Backfill provenance and correct legacy TuShare ``main_net`` values.

    The original raw JSON remains unchanged.  TuShare rows with both large and
    super-large buckets are recalculated as main-order net; the old headline
    value is retained as ``net_total``.
    """
    ensure_stock_flow_contract(con)
    con.execute("""
        UPDATE multi_source_stock_flow
        SET amount_unit = coalesce(nullif(amount_unit, ''), 'yuan'),
            origin_provider = coalesce(nullif(origin_provider, ''),
                coalesce(json_extract_string(raw_json, '$._src'), provider, 'unknown')),
            source_api = coalesce(nullif(source_api, ''),
                coalesce(json_extract_string(raw_json, '$.source_api'),
                         json_extract_string(raw_json, '$.source'), provider, 'unknown')),
            field_mapping_version = coalesce(nullif(field_mapping_version, ''), 'legacy_v1')
    """)
    legacy_tushare = con.execute("""
        SELECT count(*) FROM multi_source_stock_flow
        WHERE provider='tushare' AND flow_definition IS NULL
    """).fetchone()[0]
    con.execute("""
        UPDATE multi_source_stock_flow
        SET net_total = coalesce(net_total, main_net),
            main_net = CASE
                WHEN super_net IS NOT NULL OR large_net IS NOT NULL
                THEN coalesce(super_net, 0) + coalesce(large_net, 0)
                ELSE main_net
            END,
            flow_definition = CASE
                WHEN super_net IS NOT NULL OR large_net IS NOT NULL THEN 'main_orders_net'
                ELSE 'total_net_only'
            END,
            source_api = coalesce(nullif(source_api, ''), 'moneyflow'),
            field_mapping_version = 'stock_flow_v2'
        WHERE provider='tushare'
    """)
    con.execute("""
        UPDATE multi_source_stock_flow
        SET flow_definition = coalesce(nullif(flow_definition, ''),
            CASE WHEN provider IN ('eastmoney', 'eastmoney_market', 'kpl')
                 THEN 'main_orders_net' ELSE 'provider_main_net' END),
            field_mapping_version = coalesce(nullif(field_mapping_version, ''), 'stock_flow_v2')
        WHERE provider <> 'tushare'
    """)
    con.commit()
    return {"legacy_tushare_rows": int(legacy_tushare)}
