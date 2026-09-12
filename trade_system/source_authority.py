"""Single source-of-truth policy for production market-data datasets.

The repository intentionally keeps multiple providers for recovery and
reconciliation.  This module makes the final selection explicit so a late
fallback row cannot become authoritative merely because it has a newer
``fetched_at`` value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SourcePolicy:
    dataset: str
    canonical_relation: str
    primary: tuple[str, ...]
    fallback: tuple[str, ...]
    write_owner: str
    compatibility_only: tuple[str, ...] = ()
    notes: str = ""

    @property
    def provider_order(self) -> tuple[str, ...]:
        return self.primary + self.fallback


SOURCE_POLICIES: dict[str, SourcePolicy] = {
    "observation_quote": SourcePolicy(
        "observation_quote", "multi_source_quote", ("hithink", "hithink_quote", "hithink_official"),
        ("xiaodefa", "tushare_relay", "tencent_spot_quote", "tencent", "sina"),
        "existing_quote_collectors_read_only_observation_consumer",
        (),
        "Observation only. Source event time, receipt order and TTL must qualify before priority is applied; never an executable/account quote.",
    ),
    "market_state": SourcePolicy(
        "market_state", "daily_summary", ("kpl_market",),
        ("secondary_verified", "derived_current", "fallback"),
        "derive_market_context.py",
        ("fetch_all.py (full compatibility path)",),
        "The source_kind/as_of gate remains authoritative.",
    ),
    "kline": SourcePolicy(
        "kline", "kline", ("xiaodefa", "tushare_daily", "tushare"),
        ("tushare_relay", "baostock", "pytdx", "tencent", "sina", "kpl", "existing_core", "cache"),
        "sync_tushare_ohlc.py / TushareHistoryCollector",
        ("collect_professional_sources.py", "fetch_all.py (full compatibility path)"),
        "xiaodefa is preferred for close history; fast relay is a bounded fallback. Only a certified daily snapshot may promote to core kline.",
    ),
    "ths_concept": SourcePolicy(
        "ths_concept_daily", "ths_concept_daily", ("hithink_index_api",),
        ("ths_official_api", "ths_web", "tushare_dc_member", "browser_repair", "kpl"),
        "collect_ths_concepts_api.py",
        ("import_tushare_ths_members.py", "repair_ths_via_browser.py"),
        "HiThink official is the primary concept catalogue. A partial snapshot must never replace a complete snapshot.",
    ),
    "stock_flow": SourcePolicy(
        "stock_flow", "multi_source_stock_flow", ("eastmoney_intraday_clist", "eastmoney_intraday_clist_delay"),
        ("tushare", "tushare_relay", "kpl", "sina", "eastmoney_market", "eastmoney", "resilient", "cache"),
        "collect_intraday_stock_flow_market.py",
        ("collect_capital_flow_focus.py", "collect_multisource.py"),
        "TuShare is independent certification evidence, not additive flow.",
    ),
    "sector_flow": SourcePolicy(
        "sector_flow", "multi_source_sector_flow", ("eastmoney_sector_full",),
        ("tushare_sector_full", "tushare", "derived_ths_stock_aggregate", "kpl", "existing_core", "eastmoney", "cache"),
        "collect_intraday_sector_flow_full.py",
        ("collect_capital_flow_focus.py", "collect_sector.py"),
        "Derived THS rows retain their taxonomy and are not silently merged as raw EM rows.",
    ),
    "limit_pool": SourcePolicy(
        "limit_pool", "v_limit_pool", ("hithink", "kpl"),
        ("xiaodefa", "eastmoney", "derived"),
        "collect_realtime_limit_pool.py",
        ("fetch_all.py (full compatibility path)",),
        "Limit-up/ladder data is not a substitute for capital-flow data.",
    ),
    "auction": SourcePolicy(
        "auction", "auction_tick", ("kpl_auction_market",),
        ("tencent_qt", "auction_quote_snapshot", "kpl_legacy"),
        "collect_auction_market_daily.py",
        ("collect_auction_tick_daily.py", "collect_auction_anomaly_daily.py"),
        "One full-market request is authoritative. Tencent is an order-book snapshot fallback, never relabelled as trade ticks.",
    ),
    "index": SourcePolicy(
        "index", "index_kline", ("kpl_index_kline",),
        ("eastmoney_index", "tushare_index", "existing_core"),
        "collect_index_kline_daily.py",
        ("collect_index.py (compatibility collector)",),
        "Index source date must match the requested session; stale history cannot satisfy the close gate.",
    ),
    "lhb": SourcePolicy(
        "lhb", "lhb_list", ("kpl_lhb",),
        ("eastmoney_lhb", "tushare_lhb", "existing_core"),
        "collect_lhb_daily.py",
        ("collect_professional_sources.py",),
        "龙虎榜 is review evidence and does not block the close publication gate.",
    ),
    "chips": SourcePolicy(
        "chips", "xdf_cyq_chips", ("xiaodefa",),
        ("eastmoney_chip", "akshare_chip"),
        "trade_system/xiaodefa_source.py",
        (),
        "Chip distribution is a bounded research/review supplement; all-stock unbounded fetches are prohibited.",
    ),
    "margin": SourcePolicy(
        "margin", "xdf_margin_summary", ("xiaodefa",),
        ("tushare_margin", "eastmoney_margin"),
        "trade_system/xiaodefa_source.py",
        (),
        "Margin data is supplemental and must retain its source date and provider.",
    ),
    "qlib": SourcePolicy(
        "qlib", "qlib_candidate_pool", ("qlib_shadow",),
        ("external_qlib_file",),
        "scripts/run_qlib_research_daily.py",
        ("scripts/run_qlib_daily.py (inference worker)",),
        "QLib ranks candidates and is evaluated against later K-lines; until promotion evidence exists it cannot create execution signals.",
    ),
    "operator_outcome": SourcePolicy(
        "operator_outcome", "operator_trade_outcome", ("operator_input",),
        (),
        "trade_system/operator_outcomes.py",
        (),
        "Only reviewed human outcomes can close the QLib feedback loop; untouched templates are not data.",
    ),
}


PROVIDER_ALIASES = {
    "kpl_l2": "kpl",
    "kpl_ladder": "kpl",
    # These aliases are shared by the raw and canonical layers.  Keep the
    # THS official name intact: it is the primary provider name for the THS
    # concept policy, while ``hithink`` is the limit-pool provider name.
    "tushare_moneyflow": "tushare",
    "kpl_focus": "kpl",
}


# The authority matrix is useful only if a production plan cannot silently
# omit one of its canonical producers. Keep this map small and explicit: it
# covers the data products that determine close/review correctness, while
# optional research collectors remain outside the close contract.
PHASE_REQUIRED_TASKS: dict[str, dict[str, tuple[str, ...]]] = {
    "auction": {
        "market_state": ("collect_market_context",),
        "limit_pool": ("collect_realtime_limit_pool",),
        "auction": ("collect_auction_evidence",),
    },
    "intraday": {
        "market_state": ("collect_market_context",),
        "limit_pool": ("collect_realtime_limit_pool",),
        "stock_flow": ("collect_intraday_stock_flow_market",),
        "sector_flow": ("collect_intraday_sector_flow_full",),
    },
    "close": {
        "market_state": ("collect_market_context",),
        "kline": ("sync_tushare_close",),
        "ths_concept": ("collect_ths_concepts_api",),
        "limit_pool": (
            "collect_hithink_limit_pool_daily",
            "collect_realtime_limit_pool",
        ),
        "stock_flow": ("collect_intraday_stock_flow_market",),
        "sector_flow": ("collect_intraday_sector_flow_full",),
    },
}


def validate_production_plan(phase: str, task_names: list[str] | tuple[str, ...]) -> None:
    """Fail before execution when a core dataset lost its producer edge.

    This is a plan-level check, not a network-health check. A provider may
    still be unavailable and be handled by its documented fallback, but the
    production graph must not silently stop collecting a canonical dataset
    because a task was removed from the runner.
    """
    requirements = PHASE_REQUIRED_TASKS.get(str(phase).lower())
    if not requirements:
        return
    present = set(task_names)
    missing = {
        dataset: [task for task in tasks if task not in present]
        for dataset, tasks in requirements.items()
        if any(task not in present for task in tasks)
    }
    if missing:
        detail = "; ".join(
            f"{dataset}: {', '.join(tasks)}" for dataset, tasks in missing.items()
        )
        raise ValueError(f"production {phase} plan is missing authority producers: {detail}")


def policy(dataset: str) -> SourcePolicy:
    """Return a policy and fail loudly for an unregistered production domain."""
    try:
        return SOURCE_POLICIES[dataset]
    except KeyError as exc:
        raise KeyError(f"no source authority policy registered for {dataset!r}") from exc


def provider_rank(dataset: str, provider: Any) -> int:
    """Return a stable rank; unknown providers always lose to known ones."""
    value = str(provider or "").strip().lower()
    value = PROVIDER_ALIASES.get(value, value)
    providers = policy(dataset).provider_order
    try:
        return providers.index(value)
    except ValueError:
        return len(providers) + 100


def provider_rank_sql(dataset: str, provider_expr: str = "provider") -> str:
    """Return the SQL expression for the same rank used by ``provider_rank``.

    Provider selection used to be duplicated in flow features, review SQL,
    sector aggregation, and the compatibility store.  Keeping the SQL
    rendering beside the Python policy makes those consumers use the same
    order and keeps unknown providers below every declared provider.
    """
    providers = policy(dataset).provider_order
    normalized = f"lower(trim(coalesce({provider_expr}, '')))"
    branches: list[str] = []
    for rank, provider in enumerate(providers):
        accepted = [provider]
        accepted.extend(
            alias for alias, target in PROVIDER_ALIASES.items() if target == provider
        )
        values = ", ".join("'" + value.replace("'", "''") + "'" for value in accepted)
        branches.append(f"WHEN {normalized} IN ({values}) THEN {rank}")
    branches.append(f"ELSE {len(providers) + 100}")
    return "CASE " + " ".join(branches) + " END"


def policy_dicts() -> list[dict[str, Any]]:
    return [asdict(item) for item in SOURCE_POLICIES.values()]


def render_policy_markdown() -> str:
    lines = [
        "# Source Authority Matrix",
        "",
        "This is the production selection contract. Fallback providers remain available for recovery and reconciliation, but a newer `fetched_at` alone does not promote them over the declared primary source.",
        "",
        "| Dataset | Canonical relation | Primary | Fallback | Write owner | Compatibility only |",
        "|---|---|---|---|---|---|",
    ]
    for item in SOURCE_POLICIES.values():
        lines.append(
            f"| `{item.dataset}` | `{item.canonical_relation}` | "
            f"{', '.join(item.primary)} | {', '.join(item.fallback)} | "
            f"`{item.write_owner}` | {', '.join(item.compatibility_only) or '-'} |"
        )
    lines.extend([
        "",
        "## Rules",
        "",
        "1. Raw/provider rows stay source-separated.",
        "2. Only the write owner promotes a row into a canonical core relation.",
        "3. A fallback row must carry `is_fallback` or an equivalent provider marker.",
        "4. Independent sources are used for certification/reconciliation, not summed together.",
        "5. Compatibility scripts may be run for recovery, but must not be scheduled as a second production writer.",
        "",
    ])
    return "\n".join(lines)
