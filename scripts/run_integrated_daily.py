from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trade_system.collection_profiles import phase_tasks, resolve_phase
from trade_system.pipeline_contract import (
    CLOSE_DEFERRED_GATES,
    DEGRADABLE_EXTERNAL_STEPS,
    INFORMATIONAL_REVIEW_STEPS,
    INTRADAY_DIAGNOSTIC_STEPS,
    OPTIONAL_CLOSE_STEPS,
    RESEARCH_CHAIN_STEPS,
    REVIEW_CHAIN_STEPS,
)

CommandStep = tuple[str, list[str], bool]

# P0#2: how many calendar days back the close-phase TuShare sync scans for gaps.
# Already-synced dates are skipped via the history checkpoint, so this only fetches
# missing/failed recent sessions (e.g. a prior day the relay dropped).
TUSHARE_GAPFILL_LOOKBACK_DAYS = 10

# Audit P2 #1: close readiness / capital-flow acceptance window.  Tightened from
# 21600s (6h) to 7200s (2h) so a degraded close collector that leaves a stale
# intraday snapshot (last intraday fetch ~15:00, >2h before the 17:30 close run) is
# fail-closed (close_blocked) instead of being accepted as close-ready.  Freshly
# re-collected close data (<1h old) still passes.  The close-decision SIGNAL evidence
# window (--freshness-seconds 21600 below) is intentionally left at 6h: it spans the
# trading day for the day-outcome decision, a different concern from collection freshness.
CLOSE_READINESS_MAX_AGE_SECONDS = 7200



def _is_degradable_failure(selected_phase: str, name: str) -> bool:
    """Keep collection retries alive while fail-closing intraday signals."""
    return (
        name in DEGRADABLE_EXTERNAL_STEPS
        or (
            selected_phase == "intraday"
            and name in INTRADAY_DIAGNOSTIC_STEPS
        )
        # Missing stock-level auction evidence must block execution, but it
        # must not prevent the dashboard from showing the blocked candidates
        # and exact missing gate.
        or (selected_phase == "auction" and name == "check_data_readiness")
        or (selected_phase == "close" and name in CLOSE_DEFERRED_GATES)
    )


def command_plan(
    db_path: str,
    trade_date: str | None = None,
    *,
    include_collection: bool = False,
    max_stocks: int = 20,
    max_sectors: int = 20,
    finance_max_stocks: int = 5,
    signal_limit: int = 200,
    reports_dir: str = "reports",
    as_of_time: str | None = None,
    collection_profile: str = "full",
    phase: str | None = None,
    history_start: str = "",
    history_end: str = "",
    history_max_days: int = 0,
    include_research: bool | None = None,
) -> list[CommandStep]:
    py = sys.executable
    selected_date = trade_date or date.today().isoformat()
    # ``None`` preserves the legacy full/compatibility plan.  Explicit close
    # runs default to the operational chain only; research is opt-in so Qlib,
    # news and backtests cannot lengthen or fail the close publication.
    research_enabled = (
        phase in {None, "full"}
        if include_research is None
        else bool(include_research)
    )
    report = lambda name: str(Path(reports_dir) / name)
    steps: list[CommandStep] = []
    if include_collection and phase not in {None, "full"}:
        # Phase mode is intentionally narrow.  It is the production path for
        # auction/intraday/close runs; the legacy fan-out below remains
        # available only when callers explicitly omit ``phase`` or select
        # ``full`` for compatibility/backfill work.
        if phase == "auction":
            collection_steps = [
                ("collect_market_context", [py, "fetch_all.py", "--db", db_path, "--date", selected_date, "--only-market"], False),
                ("collect_realtime_limit_pool", [py, "scripts/collect_realtime_limit_pool.py", "--db", db_path, "--date", selected_date, "--out", report("realtime_candidate_pool_latest.md")], False),
                ("collect_auction_evidence", [py, "scripts/collect_auction_evidence.py", "--db", db_path, "--date", selected_date, "--max-stocks", str(signal_limit), "--out", report("auction_collection_latest.json")], False),
                ("build_auction_evidence", [py, "scripts/build_auction_evidence.py", "--db", db_path, "--trade-date", selected_date, "--out", report("auction_evidence_latest.md")], False),
            ]
        elif phase == "intraday":
            collection_steps = [
                ("collect_market_context", [py, "fetch_all.py", "--db", db_path, "--date", selected_date, "--only-market"], False),
                ("collect_realtime_limit_pool", [py, "scripts/collect_realtime_limit_pool.py", "--db", db_path, "--date", selected_date, "--out", report("realtime_candidate_pool_latest.md")], False),
                ("collect_intraday_stock_flow_market", [py, "scripts/collect_intraday_stock_flow_market.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_stock_flow_latest.md")], False),
                # Phase mode never runs full L2; keep candidate stock curves fresh.
                ("collect_l2_focus", [py, "scripts/collect_l2_focus.py", "--db", db_path, "--date", selected_date,
                 "--max-stocks", str(min(60, max(20, signal_limit // 3))),
                 "--total-budget-seconds", "90", "--auto-boost-if-kpl-stale",
                 "--out", report("l2_focus_collection_latest.md")], False),
                # Refresh the sector snapshot after the bounded L2 work so its
                # 10-minute readiness TTL cannot expire while L2 is running.
                ("repair_critical_integrity_pre_sector", [py, "scripts/repair_critical_integrity.py", "--db", db_path], False),
                ("collect_intraday_sector_flow_full", [py, "scripts/collect_intraday_sector_flow_full.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_sector_flow_latest.md")], False),
                ("derive_market_context", [py, "scripts/derive_market_context.py", "--db", db_path, "--date", selected_date, "--out", report("market_context_latest.json")], False),
            ]
        elif phase == "close":
            collection_steps = [
                ("collect_market_context", [py, "fetch_all.py", "--db", db_path, "--date", selected_date, "--only-market"], False),
                # P0#2: close guarantees the newest snapshot here; the following
                # physical-kline step also repairs the bounded recent-gap window
                # before copying it, so older history remains a separate batch.
                ("sync_tushare_close", [py, "scripts/backfill_2026_tushare.py", "--db", db_path,
                 "--start-date", (date.fromisoformat(selected_date) - timedelta(days=TUSHARE_GAPFILL_LOOKBACK_DAYS)).strftime("%Y%m%d"),
                 "--end-date", selected_date.replace("-", ""),
                 "--datasets", "daily,daily_basic,adj_factor,moneyflow,industry_flow", "--gap-only", "--max-days", "1",
                 "--retry-passes", "1", "--retry-delay-seconds", "2.0",
                 "--report", report("tushare_close_latest.md")], False),
                # Push TuShare daily into physical kline so data_chain / collectors
                # that still read ``kline`` see the same session as v_kline_daily.
                ("sync_tushare_ohlc_core", [py, "scripts/sync_tushare_ohlc.py", "--db", db_path,
                 "--start-date", (date.fromisoformat(selected_date) - timedelta(days=TUSHARE_GAPFILL_LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
                 "--end-date", selected_date, "--repair-close-gaps", "--repair-budget-seconds", "180"], False),
                ("refresh_ths_weekly", [py, "scripts/backfill_2026_ths_concepts.py", "--db", db_path,
                 "--start-date", selected_date.replace("-", ""), "--end-date", selected_date.replace("-", ""),
                 "--period", "week", "--mode", "full", "--max-member-pages", "0", "--max-concepts", "0",
                 "--member-source", "web", "--report", report("ths_weekly_latest.md")], False),
                ("collect_realtime_limit_pool", [py, "scripts/collect_realtime_limit_pool.py", "--db", db_path, "--date", selected_date, "--out", report("realtime_candidate_pool_latest.md")], False),
                ("collect_kpl_stock_flow_focus", [py, "scripts/collect_capital_flow_focus.py", "--db", db_path,
                 "--date", selected_date, "--max-stocks", str(signal_limit), "--max-sectors", "0",
                 "--moneyflow-only", "--no-resilient-fallback", "--total-budget-seconds", "45",
                 "--out", report("kpl_stock_flow_focus_latest.md")], False),
                ("collect_intraday_stock_flow_market", [py, "scripts/collect_intraday_stock_flow_market.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_stock_flow_latest.md")], False),
                ("collect_intraday_sector_flow_full", [py, "scripts/collect_intraday_sector_flow_full.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_sector_flow_latest.md")], False),
                ("derive_market_context", [py, "scripts/derive_market_context.py", "--db", db_path, "--date", selected_date, "--out", report("market_context_latest.json")], False),
                ("collect_finance_gapfill", [py, "collect_finance.py", "--db", db_path, "--date", selected_date, "--max-stocks", str(finance_max_stocks), "--refresh-days", "7"], False),
                # LHB was orphaned in the phase-mode migration (fetch_all.py
                # imports collect_all_lhb but the --only-market path returns
                # before any call), leaving lhb_* frozen at 2026-07-08.
                ("collect_lhb_daily", [py, "scripts/collect_lhb_daily.py", "--db", db_path, "--date", selected_date, "--out", report("lhb_collection_latest.md")], False),
                # Bidding anomalies can only be fetched after close: the KPL
                # endpoint returns the latest trading day, which only matches
                # the requested date once the session has ended.
                ("collect_auction_anomaly_daily", [py, "scripts/collect_auction_anomaly_daily.py", "--db", db_path, "--date", selected_date, "--out", report("auction_anomaly_collection_latest.md")], False),
                # Full 09:15-09:25 auction tick series (KPL returns the whole
                # day's sequence after close; the parser bug that silently
                # dropped every tick was fixed in collect_misc.collect_auction_tick).
                ("collect_auction_tick_daily", [py, "scripts/collect_auction_tick_daily.py", "--db", db_path, "--date", selected_date, "--out", report("auction_tick_collection_latest.md")], False),
                # On-the-LHB probability predictions (previously unreachable:
                # only wired behind fetch_all.py's non --only-market path).
                ("collect_advanced_lhb_daily", [py, "scripts/collect_advanced_lhb_daily.py", "--db", db_path, "--date", selected_date, "--out", report("advanced_lhb_collection_latest.md")], False),
                # Northbound capital previously lived only in the staged
                # scheduler (never invoked by the production phases), freezing
                # multi_source_observation at 2026-07-14.
                ("collect_northbound_daily", [py, "scripts/collect_northbound_daily.py", "--db", db_path, "--date", selected_date, "--out", report("northbound_collection_latest.md")], False),
                # Real index klines (full-history endpoint, idempotent) --
                # previously unreachable because the scheduler always runs
                # fetch_all.py with --only-market, which returns early.
                ("collect_index_kline_daily", [py, "scripts/collect_index_kline_daily.py", "--db", db_path, "--date", selected_date, "--out", report("index_kline_collection_latest.md")], False),
                # L2 is an intraday-only source.  After the market closes the
                # upstream endpoint normally returns an empty payload, so the
                # close phase reuses the last same-day intraday snapshot.
                ("collect_executable_quotes", [py, "scripts/collect_executable_quotes.py", "--db", db_path, "--date", selected_date,
                 "--limit", str(signal_limit), "--auto-boost-if-kpl-stale"], False),
            ]
        elif phase == "history":
            start = history_start or "20260101"
            end = history_end or selected_date.replace("-", "")
            collection_steps = [
                ("backfill_2026_tushare", [py, "scripts/backfill_2026_tushare.py", "--db", db_path, "--start-date", start, "--end-date", end, "--max-days", str(max(0, history_max_days)), "--report", report("tushare_2026_backfill_latest.md")], False),
                ("backfill_2026_ths_concepts", [py, "scripts/backfill_2026_ths_concepts.py", "--db", db_path, "--start-date", start, "--end-date", end, "--mode", "full", "--max-member-pages", "0", "--report", report("ths_2026_concepts_latest.md")], False),
                ("run_staged_after_close", [py, "scripts/run_staged_multisource.py", "--db", db_path, "--date", selected_date, "--stage", "after_close", "--start", start, "--end", end, "--budget-seconds", "300", "--report", report("multisource_after_close_latest.md")], False),
            ]
        else:
            raise ValueError(f"Unsupported collection phase: {phase}")
        steps.extend(collection_steps)
    elif include_collection:
        collection_steps = [
                (
                    "collect_market_context",
                    [py, "fetch_all.py", "--db", db_path, "--date", selected_date, "--only-market"],
                    False,
                ),
                (
                    "collect_realtime_limit_pool",
                    [
                        py,
                        "scripts/collect_realtime_limit_pool.py",
                        "--db",
                        db_path,
                        "--date",
                        selected_date,
                        "--out",
                        report("realtime_candidate_pool_latest.md"),
                    ],
                    False,
                ),
                (
                    "collect_intraday_stock_flow_market",
                    [
                        py,
                        "scripts/collect_intraday_stock_flow_market.py",
                        "--db",
                        db_path,
                        "--date",
                        selected_date,
                        "--out",
                        report("intraday_stock_flow_latest.md"),
                    ],
                    False,
                ),
                (
                    "collect_capital_flow_focus",
                    [
                        py,
                        "scripts/collect_capital_flow_focus.py",
                        "--db",
                        db_path,
                        "--date",
                        selected_date,
                        "--max-stocks",
                        str(max_stocks),
                        "--max-sectors",
                        str(max_sectors),
                        "--strict",
                        "--out",
                        report("capital_flow_freshness_latest.md"),
                    ],
                    False,
                ),
                (
                    "collect_intraday_sector_flow_full",
                    [
                        py,
                        "scripts/collect_intraday_sector_flow_full.py",
                        "--db",
                        db_path,
                        "--date",
                        selected_date,
                        "--out",
                        report("intraday_sector_flow_latest.md"),
                    ],
                    False,
                ),
                (
                    "collect_multisource_capital_flow",
                    [
                        py,
                        "scripts/run_staged_multisource.py",
                        "--db",
                        db_path,
                        "--date",
                        selected_date,
                        "--max-stocks",
                        str(max_stocks),
                        "--max-sectors",
                        str(max_sectors),
                        "--stage",
                        "close",
                        "--report",
                        report("multisource_collection_latest.md"),
                    ],
                    False,
                ),
                (
                    "collect_finance_gapfill",
                    [
                        py,
                        "collect_finance.py",
                        "--db",
                        db_path,
                        "--date",
                        selected_date,
                        "--max-stocks",
                        str(finance_max_stocks),
                    ],
                    False,
                ),
            ]
        if collection_profile == "priority":
            # The full-market collectors already persist complete snapshots.
            # Avoid the older focus/staged calls unless an operator explicitly
            # requests the compatibility profile; this prevents duplicate API
            # traffic and competing writes during the same close run.
            keep = {
                "collect_market_context",
                "collect_realtime_limit_pool",
                "collect_intraday_stock_flow_market",
                "collect_intraday_sector_flow_full",
                "collect_finance_gapfill",
            }
            collection_steps = [step for step in collection_steps if step[0] in keep]
        elif collection_profile != "full":
            raise ValueError(f"Unsupported collection profile: {collection_profile}")
        steps.extend(collection_steps)
    if phase not in {"auction", "intraday", "close", "history"}:
        phase = None
    # Narrow phases stop after their decision-window work.  The full legacy
    # report/backtest fan-out is retained for explicit full/compatibility runs.
    if phase == "auction":
        steps.extend([
            ("build_normalized_views", [py, "scripts/build_normalized_views.py", "--db", db_path], False),
            ("generate_signals", [py, "scripts/generate_signals.py", "--db", db_path, "--date", selected_date], False),
            ("generate_auction_stage_signals", [py, "scripts/generate_stage_signals.py", "--db", db_path, "--date", selected_date, "--stage", "auction_confirmation", "--run-id", "integrated_auction", "--freshness-seconds", "300", "--strict-tradability", "--allow-blocked", "--limit", str(signal_limit)], False),
            ("run_daily_operator_loop", [py, "scripts/run_daily_operator_loop.py", "--db", db_path, "--trade-date", selected_date, "--stage", "auction", "--limit", str(signal_limit)], False),
            ("check_data_readiness", [py, "scripts/check_data_readiness.py", "--db", db_path, "--date", selected_date, "--stage", "auction", "--max-age-seconds", "300", "--out", report("data_readiness_auction_latest.md")], False),
            ("generate_web_dashboard", [py, "scripts/generate_web_dashboard.py", "--db", db_path, "--date", selected_date, "--out", report("trading_dashboard_latest.html")], False),
            ("generate_trading_terminal", [py, "scripts/generate_trading_terminal.py", "--db", db_path, "--date", selected_date, "--out", report("trading_terminal_latest.html")], False),
        ])
        return steps
    if phase == "intraday":
        steps.extend([
            ("build_normalized_views", [py, "scripts/build_normalized_views.py", "--db", db_path], False),
            # Candidate-only live quotes (Tencent).  Unblocks strict tradability
            # when the full-market Eastmoney path is delayed-only.  Auto-boost
            # when KPL same-date market context is stale.
            ("collect_executable_quotes", [py, "scripts/collect_executable_quotes.py", "--db", db_path, "--date", selected_date, "--limit", str(signal_limit), "--auto-boost-if-kpl-stale"], False),
            ("audit_multisource_readiness", [py, "scripts/audit_multisource_readiness.py", "--db", db_path, "--as-of", selected_date, "--out", report("multisource_readiness_intraday_latest.md")], False),
            ("check_capital_flow_health", [py, "scripts/check_capital_flow_health.py", "--db", db_path, "--date", selected_date, "--max-age-seconds", "600", "--min-coverage-pct", "99.5", "--out", report("capital_flow_freshness_latest.md")], False),
            ("generate_signals", [py, "scripts/generate_signals.py", "--db", db_path, "--date", selected_date], False),
            ("generate_intraday_stage_signals", [py, "scripts/generate_stage_signals.py", "--db", db_path, "--date", selected_date, "--stage", "intraday_strength", "--run-id", "integrated_intraday", "--freshness-seconds", "600", "--strict-tradability", "--limit", str(signal_limit)], False),
            ("run_daily_operator_loop", [py, "scripts/run_daily_operator_loop.py", "--db", db_path, "--trade-date", selected_date, "--stage", "intraday", "--limit", str(signal_limit)], False),
            ("check_data_readiness", [py, "scripts/check_data_readiness.py", "--db", db_path, "--date", selected_date, "--stage", "intraday", "--max-age-seconds", "600", "--out", report("data_readiness_intraday_latest.md")], False),
            ("audit_p3_candidates", [py, "scripts/audit_p3_candidates.py", "--db", db_path, "--date", selected_date, "--out", report("p3_candidate_audit_intraday_latest.md")], False),
            ("generate_web_dashboard", [py, "scripts/generate_web_dashboard.py", "--db", db_path, "--date", selected_date, "--out", report("trading_dashboard_latest.html")], False),
            ("generate_trading_terminal", [py, "scripts/generate_trading_terminal.py", "--db", db_path, "--date", selected_date, "--out", report("trading_terminal_latest.html")], False),
        ])
        return steps
    if phase == "history":
        steps.extend([
            ("build_data_catalog", [py, "scripts/build_data_catalog.py", "--db", db_path], False),
            ("audit_data_quality", [py, "scripts/audit_data_quality.py", "--db", db_path, "--schema", "schema.py", "--out", report("data_quality_history_latest.md")], False),
        ])
        return steps

    close_stage_command = [
        py,
        "scripts/generate_stage_signals.py",
        "--db",
        db_path,
        "--date",
        selected_date,
        "--stage",
        "close_decision",
        "--run-id",
        "integrated_close",
        "--freshness-seconds",
        "21600",
        "--strict-tradability",
        "--limit",
        str(signal_limit),
    ]
    if as_of_time:
        close_stage_command.extend(["--as-of", as_of_time])
    capital_health_command = [
        py, "scripts/check_capital_flow_health.py", "--db", db_path,
        "--date", selected_date, "--max-age-seconds", str(CLOSE_READINESS_MAX_AGE_SECONDS),
        "--min-coverage-pct", "99.5", "--out", report("capital_flow_freshness_latest.md"),
    ]
    readiness_command = [
        py, "scripts/check_data_readiness.py", "--db", db_path,
        "--date", selected_date, "--stage", "close",
        "--max-age-seconds", str(CLOSE_READINESS_MAX_AGE_SECONDS),
        "--out", report("data_readiness_latest.md"),
    ]
    acceptance_command = [
        py, "scripts/audit_p0_p3_acceptance.py", "--db", db_path,
        "--date", selected_date, "--reports-dir", reports_dir,
        "--max-age-seconds", str(CLOSE_READINESS_MAX_AGE_SECONDS),
        "--out", report("p0_p3_acceptance_latest.md"),
    ]
    if as_of_time:
        capital_health_command.extend(["--as-of", as_of_time])
        readiness_command.extend(["--as-of", as_of_time])
        acceptance_command.extend(["--as-of", as_of_time])
    steps.extend([
        ("repair_critical_integrity", [py, "scripts/repair_critical_integrity.py", "--db", db_path], False),
        ("build_normalized_views", [py, "scripts/build_normalized_views.py", "--db", db_path], False),
        ("ensure_operational_indexes", [py, "scripts/ensure_operational_indexes.py", "--db", db_path], False),
        (
            "audit_multisource_readiness",
            [py, "scripts/audit_multisource_readiness.py", "--db", db_path, "--as-of", selected_date, "--out", report("multisource_readiness_latest.md")],
            False,
        ),
        (
            "audit_stock_flow_contract",
            [py, "scripts/audit_stock_flow_contract.py", "--db", db_path,
             "--out", report("stock_flow_contract_audit_latest.md"), "--date", selected_date],
            False,
        ),
        (
            "reconcile_independent_stock_flow",
            [py, "scripts/reconcile_independent_stock_flow.py", "--db", db_path,
             "--date", selected_date,
             "--out", report("independent_stock_flow_reconciliation_latest.md")],
            False,
        ),
        ("build_operator_views", [py, "scripts/build_operator_views.py", "--db", db_path], False),
        (
            "build_auction_evidence",
            [py, "scripts/build_auction_evidence.py", "--db", db_path, "--trade-date", selected_date, "--out", report("auction_evidence_latest.md")],
            False,
        ),
        (
            "check_capital_flow_health",
            capital_health_command,
            False,
        ),
        # The legacy aggregate signal is diagnostic only; it must not turn a
        # missing/stale data chain into a green production run.
        ("generate_signals", [py, "scripts/generate_signals.py", "--db", db_path, "--date", selected_date], False),
        (
            "generate_close_stage_signals",
            close_stage_command,
            False,
        ),
        (
            "create_operator_outcome_template",
            [
                py,
                "scripts/create_operator_outcome_template.py",
                "--db",
                db_path,
                "--date",
                selected_date,
                "--out",
                report(f"operator_outcomes_template_{selected_date}.csv"),
            ],
            False,
        ),
        (
            "run_daily_operator_loop",
            [py, "scripts/run_daily_operator_loop.py", "--db", db_path, "--trade-date", selected_date, "--stage", "close"],
            False,
        ),
        (
            "check_data_readiness",
            readiness_command,
            False,
        ),
        (
            "run_stage_backtest",
            [py, "scripts/run_stage_backtest.py", "--db", db_path, "--out", report("stage_backtest_latest.md")],
            False,
        ),
        (
            "run_daily_review_statistics",
            [py, "scripts/run_daily_review_statistics.py", "--db", db_path, "--out", report("daily_review_statistics_latest.md")],
            False,
        ),
        ("run_operator_backtest", [py, "scripts/run_operator_backtest.py", "--db", db_path, "--out", report("operator_backtest_latest.md")], True),
        ("build_data_catalog", [py, "scripts/build_data_catalog.py", "--db", db_path], False),
        (
            "audit_p2_gaps",
            [py, "scripts/audit_p2_gaps.py", "--db", db_path, "--out", report("p2_gap_audit_latest.md")],
            False,
        ),
        (
            "run_news_radar",
            [py, "scripts/run_news_radar.py", "--db", db_path, "--trade-date", selected_date, "--out", report("news_radar_latest.md")],
            True,
        ),
        (
            "run_api_research_events",
            [py, "scripts/run_api_research_events.py", "--db", db_path, "--trade-date", selected_date, "--out", report("api_research_events_latest.md")],
            False,
        ),
        ("build_research_snapshot", [py, "scripts/build_research_snapshot.py", "--db", db_path, "--out", report("research_snapshot_latest.md")], False),
        ("run_strategy_scan", [py, "scripts/run_strategy_scan.py", "--db", db_path, "--out", report("strategy_scan_latest.md")], False),
        (
            "run_strategy_result_backtest",
            [py, "scripts/run_strategy_result_backtest.py", "--db", db_path, "--out", report("strategy_backtest_latest.md")],
            False,
        ),
        *(
            [
                (
                    "build_flow_features",
                    [
                        py,
                        "scripts/build_flow_features.py",
                        "--db",
                        db_path,
                        "--end-date",
                        selected_date,
                        "--out",
                        report("flow_features_latest.json"),
                    ],
                    False,
                ),
                (
                    "export_qlib_features_close",
                    [
                        py,
                        "scripts/export_qlib_features.py",
                        "--db",
                        db_path,
                        "--start-date",
                        "2024-01-01",
                        "--end-date",
                        selected_date,
                        "--out",
                        report("qlib_features_2026_exec.parquet"),
                        "--format",
                        "parquet",
                        "--label-mode",
                        "t1_exec",
                    ],
                    False,
                )
            ]
            if phase in {None, "full", "close"}
            else []
        ),
        ("evaluate_qlib_shadow", [py, "scripts/evaluate_qlib_shadow.py", "--db", db_path, "--out", report("qlib_shadow_latest.md")], False),
        (
            "run_qlib_daily",
            [
                py,
                "scripts/run_qlib_daily.py",
                "--db",
                db_path,
                "--trade-date",
                selected_date,
                "--allow-shadow",
                "--out",
                report("qlib_daily_latest.json"),
            ],
            False,
        ),
        (
            "generate_operator_reports",
            [py, "scripts/generate_operator_reports.py", "--db", db_path, "--trade-date", selected_date, "--out", report("operator_report_latest.md")],
            False,
        ),
        (
            "generate_daily_review",
            [py, "scripts/generate_daily_review.py", "--db", db_path, "--trade-date", selected_date, "--out", report("daily_review_latest.md")],
            False,
        ),
        (
            "generate_daily_review_web",
            [py, "scripts/generate_daily_review_web.py", "--db", db_path, "--trade-date", selected_date, "--out", report("daily_review_latest.html")],
            False,
        ),
        (
            "build_ai_review_snapshot",
            [
                py,
                "scripts/generate_ai_review_snapshot.py",
                "--db",
                db_path,
                "--trade-date",
                selected_date,
                "--out",
                report("ai_review_facts_latest.json"),
            ],
            False,
        ),
        (
            "audit_p0_p3_acceptance",
            acceptance_command,
            False,
        ),
        (
            "audit_p3_candidates",
            [py, "scripts/audit_p3_candidates.py", "--db", db_path, "--date", selected_date, "--out", report("p3_candidate_audit_latest.md")],
            False,
        ),
        ("report_real_data_backfill", [py, "scripts/report_real_data_backfill.py", "--db", db_path, "--out", report("real_data_backfill_latest.md")], False),
        (
            "audit_data_quality",
            [py, "scripts/audit_data_quality.py", "--db", db_path, "--schema", "schema.py", "--out", report("data_quality_latest.md")],
            False,
        ),
        ("build_empty_table_catalog", [py, "scripts/build_empty_table_catalog.py", "--db", db_path, "--out", report("empty_table_catalog_latest.md")], False),
        (
            "assess_data_chains",
            [py, "scripts/assess_data_chains.py", "--db", db_path, "--date", selected_date, "--out", report("data_chain_status_latest.md")],
            False,
        ),
        (
            "generate_professional_reports",
            [py, "scripts/generate_professional_reports.py", "--db", db_path, "--date", selected_date, "--out-dir", reports_dir],
            False,
        ),
        (
            "generate_web_dashboard",
            [py, "scripts/generate_web_dashboard.py", "--db", db_path, "--date", selected_date, "--out", report("trading_dashboard_latest.html")],
            False,
        ),
        (
            "generate_trading_terminal",
            [py, "scripts/generate_trading_terminal.py", "--db", db_path, "--date", selected_date, "--out", report("trading_terminal_latest.html")],
            False,
        ),
    ])
    if not research_enabled and phase in {None, "full", "close"}:
        steps = [step for step in steps if step[0] not in RESEARCH_CHAIN_STEPS]
    return steps


def _script_exists(cmd: list[str]) -> bool:
    if len(cmd) < 2 or not cmd[1].startswith("scripts/"):
        return True
    return (ROOT / cmd[1]).exists()


def _command_option(cmd: list[str], name: str, default: str = "") -> str:
    try:
        index = cmd.index(name)
    except ValueError:
        return default
    return str(cmd[index + 1]) if index + 1 < len(cmd) else default


def _run_report_in_process(name: str, cmd: list[str]) -> int:
    """Run the two read/render-only daily reports without a child process.

    Collection, signal generation and database migrations intentionally remain
    subprocess tasks until their write/lock boundaries are measured and
    migrated separately. Keeping this optimization to read/render tasks
    removes interpreter churn without changing the data or gate contract.
    """
    db_path = _command_option(cmd, "--db")
    trade_date = _command_option(cmd, "--trade-date")
    out_path = _command_option(cmd, "--out")
    if name == "generate_daily_review":
        from trade_system.daily_review import build_daily_review_context, write_daily_review

        context = build_daily_review_context(db_path, trade_date or None)
        path = write_daily_review(db_path, out_path, context["trade_date"])
        print(f"daily_review_report={path}")
        print(f"trade_date={context['trade_date']}")
        print(f"plans={len(context.get('plans', []))}")
        print(f"journal={len(context.get('journal', []))}")
        return 0
    if name == "generate_daily_review_web":
        from trade_system.review_web import write_review_web

        path = write_review_web(db_path, out_path, trade_date or None)
        print(f"review_web={path}")
        return 0
    raise ValueError(f"unsupported in-process report task: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the integrated surviving stock_data daily workflow.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument(
        "--phase",
        choices=("auto", "auction", "intraday", "close", "history", "full"),
        default="auto",
        help="Decision window. auto uses local Asia/Shanghai time; full is legacy compatibility fan-out.",
    )
    parser.add_argument("--history-start", default="", help="History phase start date, YYYYMMDD.")
    parser.add_argument("--history-end", default="", help="History phase end date, YYYYMMDD.")
    parser.add_argument("--history-max-days", type=int, default=0, help="Bound TuShare history work for this invocation; 0 means checkpointed remaining dates.")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--max-sectors", type=int, default=20)
    parser.add_argument("--finance-max-stocks", type=int, default=5)
    parser.add_argument(
        "--signal-limit", type=int, default=200,
        help="Max candidates processed per signal stage; the auction/KPL-focus "
             "evidence caps are aligned to it (P1-3) so the full limit-up pool is "
             "evaluated, not just the first 20.",
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--step-timeout", type=int, default=900,
        help="Per-step wall-clock timeout in seconds (default 900); pass 0 only for a manual, unbounded run.",
    )
    parser.add_argument("--as-of", default="", help="ISO timestamp used by the close-stage cutoff gate.")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument(
        "--collection-profile",
        choices=("priority", "full"),
        default="priority",
        help="priority avoids duplicate focus/staged collection; full keeps the compatibility fan-out",
    )
    parser.add_argument(
        "--include-research",
        action="store_true",
        help="Include the optional Qlib/news/backtest research chain; close defaults to operational review only.",
    )
    parser.add_argument(
        "--subprocess-reports",
        action="store_true",
        help="Keep daily markdown/HTML report rendering in child processes for compatibility.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    selected_phase = resolve_phase(args.phase)
    if not args.skip_collect and args.trade_date != date.today().isoformat() and selected_phase != "history":
        print(
            "Historical integrated runs must use --skip-collect; some upstream endpoints "
            "are current snapshots and cannot be relabeled safely.",
            file=sys.stderr,
        )
        return 2
    if (
        not args.skip_collect
        and not args.dry_run
        and selected_phase in {"auction", "intraday", "close"}
    ):
        from trade_system.trading_calendar import ensure_trading_session_status

        session = ensure_trading_session_status(args.db, args.trade_date)
        if session.state == "closed":
            print(
                f"MARKET_CLOSED trade_date={args.trade_date} "
                f"source={session.source} reason={session.reason}"
            )
            return 0
        if session.state != "open":
            print(
                f"MARKET_CALENDAR_UNVERIFIED trade_date={args.trade_date} "
                f"source={session.source} reason={session.reason}",
                file=sys.stderr,
            )
            return 2

    run_id = args.run_id or f"{args.trade_date.replace('-', '')}_{datetime.now().strftime('%H%M%S')}_{os.getpid()}"
    # P0: every report-producing subprocess writes into an isolated run staging
    # directory.  Root ``*_latest`` files remain the last published snapshot
    # until the complete run reaches its final status.
    artifact_reports_dir = args.reports_dir
    if not args.dry_run:
        artifact_reports_dir = str(Path(args.reports_dir).resolve() / ".staging" / run_id)
        Path(artifact_reports_dir).mkdir(parents=True, exist_ok=True)
    plan = command_plan(
        args.db,
        args.trade_date,
        include_collection=not args.skip_collect,
        max_stocks=args.max_stocks,
        max_sectors=args.max_sectors,
        finance_max_stocks=args.finance_max_stocks,
        signal_limit=args.signal_limit,
        reports_dir=artifact_reports_dir,
        as_of_time=args.as_of or None,
        collection_profile=args.collection_profile,
        phase=selected_phase,
        history_start=args.history_start,
        history_end=args.history_end,
        history_max_days=args.history_max_days,
        include_research=True if args.include_research else None,
    )
    if args.dry_run:
        print(f"COLLECTION_PHASE={selected_phase}")
        if selected_phase in {"auction", "intraday", "close", "history"}:
            for task in phase_tasks(selected_phase):
                cadence = f"{task.cadence_seconds}s" if task.cadence_seconds else "manual/checkpoint"
                print(f"SOURCE {task.name}: provider={task.source} cadence={cadence} purpose={task.purpose}")
        for name, cmd, optional in plan:
            print(f"RUN {name}: {' '.join(cmd)} optional={str(optional).lower()}")
        return 0

    from trade_system.pipeline_runtime import (
        LatestReportTransaction,
        PipelineAlreadyRunning,
        PipelineLock,
        RunManifest,
        reap_stale_run_manifests,
        prune_run_reports,
    )
    from trade_system.pipeline_audit import ensure_pipeline_task_audit, record_pipeline_task

    ensure_pipeline_task_audit(args.db)
    manifest = RunManifest(args.reports_dir, run_id, args.trade_date, selected_phase)
    report_tx = LatestReportTransaction(args.reports_dir, run_id, artifact_reports_dir)
    try:
        with PipelineLock(args.db, run_id):
            reaped = reap_stale_run_manifests(
                args.reports_dir,
                exclude_run_id=run_id,
            )
            if reaped:
                print(f"REAP_STALE_MANIFESTS run_ids={','.join(reaped)}")
            report_tx.begin()
            degraded_steps = []
            chain_failed_steps = []
            informational_steps = []
            for name, cmd, optional in plan:
                if selected_phase in {"auction", "intraday", "close"} and name in {item.name for item in phase_tasks(selected_phase)}:
                    from trade_system.collection_profiles import task_due
                    due, reason = task_due(args.db, args.trade_date, name, phase=selected_phase)
                    if not due:
                        print(f"SKIP fresh {name}: {reason}")
                        manifest.add_step(name, "skipped", cmd, reason=reason)
                        record_pipeline_task(
                            args.db, run_id=run_id, trade_date=args.trade_date,
                            phase=selected_phase, task_name=name, status="skipped",
                            reason=reason,
                        )
                        continue
                if optional and not _script_exists(cmd):
                    print(f"SKIP optional {name}: {cmd[1]} is not available yet")
                    manifest.add_step(name, "skipped", cmd, reason="missing_optional_script")
                    record_pipeline_task(
                        args.db, run_id=run_id, trade_date=args.trade_date,
                        phase=selected_phase, task_name=name, status="skipped",
                        reason="missing_optional_script",
                    )
                    continue
                print(f"RUN {name}: {' '.join(cmd)}")
                started = datetime.now()
                # A5: record the step as running BEFORE launching it so a hung or
                # externally-killed step is still visible in the manifest/audit.  The
                # 2026-07-27 close manifest froze at the first step because records
                # were only written after the subprocess returned.
                manifest.upsert_step(
                    name, "running", cmd,
                    started_at=started.isoformat(timespec="seconds"),
                )
                record_pipeline_task(
                    args.db, run_id=run_id, trade_date=args.trade_date,
                    phase=selected_phase, task_name=name, status="running",
                    started_at=started,
                )
                timed_out = False
                try:
                    if not args.subprocess_reports and name in {"generate_daily_review", "generate_daily_review_web"}:
                        return_code = _run_report_in_process(name, cmd)
                    else:
                        completed = subprocess.run(
                            cmd, cwd=ROOT, check=False,
                            timeout=(args.step_timeout if args.step_timeout > 0 else None),
                        )
                        return_code = completed.returncode
                except subprocess.TimeoutExpired:
                    # subprocess.run kills the child before raising; this bounds a
                    # step that would otherwise hang the whole pipeline.
                    timed_out = True
                    return_code = -1
                duration = round((datetime.now() - started).total_seconds(), 3)
                is_degradable = _is_degradable_failure(selected_phase, name)
                if return_code == 0:
                    status = "completed"
                    reason = ""
                elif name in INFORMATIONAL_REVIEW_STEPS:
                    status = "warning"
                    reason = "operator_readiness_gate_not_passed"
                elif selected_phase == "close" and name in OPTIONAL_CLOSE_STEPS:
                    status = "warning"
                    reason = "optional_capability_unavailable"
                elif is_degradable:
                    status = "degraded"
                    reason = (f"step_timeout_after_{args.step_timeout}s" if timed_out
                              else "degraded_external_step")
                else:
                    status = "failed"
                    reason = (f"step_timeout_after_{args.step_timeout}s" if timed_out else "")
                manifest.upsert_step(
                    name,
                    status,
                    cmd,
                    return_code=return_code,
                    duration_seconds=duration,
                    reason=reason,
                )
                record_pipeline_task(
                    args.db, run_id=run_id, trade_date=args.trade_date,
                    phase=selected_phase, task_name=name, status=status,
                    return_code=return_code, started_at=started,
                    finished_at=datetime.now(), duration_seconds=duration,
                    reason=reason,
                )
                if return_code != 0:
                    if status == "degraded":
                        degraded_steps.append(name)
                        print(f"CONTINUE degraded external step {name}: return_code={return_code}{' (timeout)' if timed_out else ''}")
                        continue
                    if status == "warning":
                        informational_steps.append(name)
                        print(f"CONTINUE informational review gate {name}: return_code={return_code}")
                        continue
                    # P2-4: RESEARCH and REVIEW chain steps are failure-isolated -- a
                    # failure is recorded but does NOT abort the pipeline or roll back
                    # already-generated reports, so a research failure cannot block the
                    # review/dashboard chain.  DATA steps remain fail-fast.
                    if name in RESEARCH_CHAIN_STEPS or name in REVIEW_CHAIN_STEPS:
                        chain_failed_steps.append(name)
                        chain = "research" if name in RESEARCH_CHAIN_STEPS else "review"
                        print(f"CONTINUE chain-isolated {chain} step {name}: return_code={return_code}{' (timeout)' if timed_out else ''}")
                        continue
                    raise subprocess.CalledProcessError(return_code, cmd)
            close_blocked = selected_phase == "close" and bool(degraded_steps)
            chain_failed = bool(chain_failed_steps)
            final_status = (
                "completed_blocked"
                if close_blocked
                else "completed_with_chain_failure"
                if chain_failed
                else "completed_with_degradation"
                if degraded_steps
                else "completed_with_warnings"
                if informational_steps
                else "completed"
            )
            failed_steps = ",".join(degraded_steps + chain_failed_steps)
            manifest.finish(final_status, failed_steps or None, warnings=informational_steps or None)
            report_tx.commit(manifest.run_dir)
            prune_run_reports(args.reports_dir, keep=30)
            print(f"RUN_COMPLETE run_id={run_id} status={final_status} degraded={','.join(degraded_steps)} chain_failed={','.join(chain_failed_steps)} warnings={','.join(informational_steps)} manifest={manifest.path}")
            # A close run that published its reports but is blocked by a data
            # gate is an operationally completed run, not a process crash.
            # The manifest/readiness report carries the blocked state; keeping
            # exit code 0 prevents Task Scheduler from labelling a published
            # fail-closed review as an infrastructure failure.  Other data or
            # chain failures remain non-zero.
            if final_status == "completed_blocked":
                return 0
            return 2 if (bool(degraded_steps) or chain_failed) else 0
    except PipelineAlreadyRunning as exc:
        report_tx.cleanup_staging()
        manifest.finish("blocked", str(exc))
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        if report_tx.snapshot_dir.exists():
            retained = report_tx.rollback(manifest.run_dir)
            if retained:
                print(
                    f"RUN_FAILED_REPORTS run_id={run_id} retained={','.join(sorted(retained))}",
                    file=sys.stderr,
                )
        else:
            report_tx.cleanup_staging()
        manifest.finish("failed", str(exc))
        print(f"RUN_FAILED run_id={run_id} error={exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
