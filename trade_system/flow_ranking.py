"""Canonical multi-source capital-flow ranking helpers.

Multi-provider rows are retained for audit/reconciliation, but operator-facing
rankings and dashboards must pick **one canonical row per code** with a stable
provider priority, and must not surface near-market mega baskets as "mainline".
"""

from __future__ import annotations

from trade_system.source_authority import policy, provider_rank, provider_rank_sql

# Compatibility mapping for callers that display a score.  The order itself
# is owned by source_authority; this map must never become a second policy.
STOCK_FLOW_PROVIDER_PRIORITY: dict[str, int] = {
    provider: 1000 - rank
    for rank, provider in enumerate(policy("stock_flow").provider_order)
}
STOCK_FLOW_PROVIDER_PRIORITY.update({
    alias: STOCK_FLOW_PROVIDER_PRIORITY[target]
    for alias, target in {
        "tushare_moneyflow": "tushare",
        "kpl_focus": "kpl",
    }.items()
})

# Sector types preferred for theme/mainline ranking (higher first).
SECTOR_TYPE_PRIORITY: dict[str, int] = {
    "ths_concept": 100,
    "ths_concept_derived": 80,
    "em_industry": 70,
    "tushare_dc_sector": 50,
    "kpl_sector": 40,
}

# Name fragments that describe nearly the whole market or index membership.
# These dominate absolute main_net and drown out tradeable themes.
MEGA_SECTOR_NAME_MARKERS: tuple[str, ...] = (
    "融资融券",
    "深股通",
    "沪股通",
    "港股通",
    "MSCI",
    "富时罗素",
    "标普道琼斯",
    "沪深300",
    "中证500",
    "中证1000",
    "中证100",
    "上证50",
    "上证180",
    "深成500",
    "创业板综",
    "创业板指",
    "科创50",
    "科创板综",
    "全A",
    "A股",
    "微盘股",
    "中特估",
    "央企",
    "国企改革",
    "证金持股",
    "机构重仓",
    "社保重仓",
    "基金重仓",
    "券商重仓",
    "HS300",
    "CSI",
)


def stock_provider_rank(provider: str | None) -> int:
    raw = str(provider or "").strip().lower()
    if not raw:
        return 0
    # Keep the public score shape, but resolve the order through the one
    # authority matrix.  Higher scores still win for existing callers.
    return max(1, 1000 - provider_rank("stock_flow", raw))


def sector_type_rank(sector_type: str | None) -> int:
    raw = str(sector_type or "").strip().lower()
    if raw in SECTOR_TYPE_PRIORITY:
        return SECTOR_TYPE_PRIORITY[raw]
    return 30


def is_mega_sector_name(name: str | None) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    return any(marker in text for marker in MEGA_SECTOR_NAME_MARKERS)


def stock_flow_rank_sql(
    *,
    date_param_placeholder: str = "?",
    alias: str = "sf",
) -> str:
    """Return SQL selecting one canonical stock-flow row per stock for a date.

    Caller binds the trade date once for the ``source_date`` filter.
    """
    provider_order = provider_rank_sql("stock_flow", "provider")
    return f"""
        SELECT {alias}.*
        FROM (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY stock_code
                    ORDER BY
                        {provider_order} ASC,
                        fetched_at DESC NULLS LAST
                ) AS _rn
            FROM multi_source_stock_flow
            WHERE source_date = CAST({date_param_placeholder} AS DATE)
              AND main_net IS NOT NULL
              AND coalesce(is_stale, false) = false
        ) {alias}
        WHERE {alias}._rn = 1
    """


def sector_flow_rank_sql(
    *,
    date_param_placeholder: str = "?",
    alias: str = "sec",
    exclude_mega: bool = True,
) -> str:
    """Return SQL selecting one canonical sector-flow row per sector for a date."""
    mega_filter = ""
    if exclude_mega:
        # Keep SQL readable; markers are project constants, not user input.
        clauses = " OR ".join(
            f"coalesce(sector_name, '') LIKE '%{marker}%'" for marker in MEGA_SECTOR_NAME_MARKERS
        )
        mega_filter = f"AND NOT ({clauses})"
    provider_order = provider_rank_sql("sector_flow", "provider")
    return f"""
        SELECT {alias}.*
        FROM (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY sector_code
                    ORDER BY
                        {provider_order} ASC,
                        CASE
                            WHEN lower(coalesce(sector_type, '')) = 'ths_concept' THEN 100
                            WHEN lower(coalesce(sector_type, '')) = 'ths_concept_derived' THEN 80
                            WHEN lower(coalesce(sector_type, '')) = 'em_industry' THEN 70
                            WHEN lower(coalesce(sector_type, '')) = 'tushare_dc_sector' THEN 50
                            ELSE 30
                        END DESC,
                        fetched_at DESC NULLS LAST
                ) AS _rn
            FROM multi_source_sector_flow
            WHERE source_date = CAST({date_param_placeholder} AS DATE)
              AND main_net IS NOT NULL
              AND coalesce(is_stale, false) = false
              {mega_filter}
        ) {alias}
        WHERE {alias}._rn = 1
    """
