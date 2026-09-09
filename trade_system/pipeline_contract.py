"""Shared task classification for the integrated daily pipeline.

Keeping these sets outside the 1,000-line CLI runner makes the execution
contract inspectable without importing the runner or duplicating its policy.
The runner still re-exports the names for compatibility with existing tests
and operators.
"""

from __future__ import annotations


DEGRADABLE_EXTERNAL_STEPS = {
    "collect_market_context",
    "check_kpl_connectivity",
    "sync_tushare_close",
    "sync_tushare_ohlc_core",
    "refresh_ths_weekly",
    "collect_realtime_limit_pool",
    "collect_hithink_limit_pool_daily",
    "collect_auction_evidence",
    "collect_auction_market_daily",
    "collect_kpl_stock_flow_focus",
    "collect_intraday_stock_flow_market",
    "collect_intraday_sector_flow_full",
    "collect_executable_quotes",
    "collect_l2_focus",
    "collect_lhb_daily",
    "collect_auction_anomaly_daily",
    "collect_auction_tick_daily",
    "collect_advanced_lhb_daily",
    "collect_index_kline_daily",
    "derive_market_context",
    "collect_finance_gapfill",
    "collect_capital_flow_focus",
    "collect_multisource_capital_flow",
    "backfill_2026_tushare",
    "backfill_2026_ths_concepts",
    "run_staged_after_close",
    "run_news_radar",
    "run_api_research_events",
}

OPTIONAL_CLOSE_STEPS = {"collect_northbound_daily"}

INFORMATIONAL_REVIEW_STEPS = {
    "audit_p0_p3_acceptance",
    "check_capital_flow_health",
    "audit_source_conflicts",
    # This step must run in the production close chain.  A warning keeps the
    # review publishable but leaves flow certification closed.
    "reconcile_independent_stock_flow",
    # xiaodefa is supplemental evidence. Empty/late batches must be visible
    # and retried without being treated as a successful close input.
    "collect_xiaodefa",
}

INTRADAY_DIAGNOSTIC_STEPS = {
    "audit_multisource_readiness",
    "check_capital_flow_health",
    "check_data_readiness",
    "generate_intraday_stage_signals",
    "audit_p3_candidates",
}

CLOSE_DEFERRED_GATES = {
    "check_data_readiness",
    "generate_signals",
}

RESEARCH_CHAIN_STEPS = {
    "audit_stock_flow_contract",
    "run_stage_backtest",
    "run_operator_backtest",
    "build_data_catalog",
    "audit_p2_gaps",
    "run_news_radar",
    "run_api_research_events",
    "build_research_snapshot",
    "run_strategy_scan",
    "run_strategy_result_backtest",
    "build_flow_features",
    "export_qlib_features_close",
    "evaluate_qlib_shadow",
    "run_qlib_daily",
    "build_ai_review_snapshot",
    "audit_data_quality",
    "build_empty_table_catalog",
}

REVIEW_CHAIN_STEPS = {
    "collect_review_supplement",
    "generate_health_trend",
    "generate_cycle_analytics",
    "generate_signal_attribution",
    "audit_multisource_readiness",
    "create_operator_outcome_template",
    "run_daily_operator_loop",
    "run_daily_review_statistics",
    "generate_operator_reports",
    "generate_daily_review",
    "generate_daily_review_web",
    "audit_p0_p3_acceptance",
    "audit_p3_candidates",
    "report_real_data_backfill",
    "assess_data_chains",
    "generate_professional_reports",
    "generate_web_dashboard",
    "generate_trading_terminal",
}
