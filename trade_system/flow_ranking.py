"""Canonical multi-source capital-flow ranking helpers.

Multi-provider rows are retained for audit/reconciliation, but operator-facing
rankings and dashboards must pick **one canonical row per code** with a stable
provider priority, and must not surface near-market mega baskets as "mainline".
"""

from __future__ import annotations

# Higher score wins when choosing the canonical provider for a stock.
STOCK_FLOW_PROVIDER_PRIORITY: dict[str, int] = {
    # Live Eastmoney clist (if ever landed without delay tag).
    "eastmoney_intraday_clist": 100,
    "eastmoney_market": 95,
    # Primary production path today: delayed full-market clist.
    "eastmoney_intraday_clist_delay": 90,
    "eastmoney": 80,
    "tushare": 70,
    "tushare_moneyflow": 70,
    # KPL focus snapshots only cover a tiny candidate set — never promote them
    # over full-market providers when ranking the whole market.
    "kpl": 40,
    "kpl_focus": 40,
}

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
    if raw in STOCK_FLOW_PROVIDER_PRIORITY:
        return STOCK_FLOW_PROVIDER_PRIORITY[raw]
    for key, score in STOCK_FLOW_PROVIDER_PRIORITY.items():
        if key in raw:
            return score
    # Unknown providers beat KPL-focus-only noise but lose to known full-market.
    return 55


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
    # CASE expression mirrors STOCK_FLOW_PROVIDER_PRIORITY for DuckDB-only paths.
    return f"""
        SELECT {alias}.*
        FROM (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY stock_code
                    ORDER BY
                        CASE
                            WHEN lower(coalesce(provider, '')) = 'eastmoney_intraday_clist' THEN 100
                            WHEN lower(coalesce(provider, '')) = 'eastmoney_market' THEN 95
                            WHEN lower(coalesce(provider, '')) = 'eastmoney_intraday_clist_delay' THEN 90
                            WHEN lower(coalesce(provider, '')) LIKE 'eastmoney%' THEN 80
                            WHEN lower(coalesce(provider, '')) LIKE 'tushare%' THEN 70
                            WHEN lower(coalesce(provider, '')) LIKE 'kpl%' THEN 40
                            ELSE 55
                        END DESC,
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
    return f"""
        SELECT {alias}.*
        FROM (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY sector_code
                    ORDER BY
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
