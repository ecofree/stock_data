from datetime import date
import sys

import duckdb

from scripts.run_integrated_daily import (
    _is_degradable_failure,
    command_plan,
    main,
)


def test_integrated_daily_command_plan_contains_required_steps():
    steps = command_plan("kpl_data.duckdb")
    names = [step[0] for step in steps]
    assert names == [
        "repair_critical_integrity",
        "build_normalized_views",
        "ensure_operational_indexes",
        "audit_multisource_readiness",
        "audit_stock_flow_contract",
        "reconcile_independent_stock_flow",
        "build_operator_views",
        "build_auction_evidence",
        "check_capital_flow_health",
        "generate_signals",
        "generate_close_stage_signals",
        "create_operator_outcome_template",
        "run_daily_operator_loop",
        "check_data_readiness",
        "run_stage_backtest",
        "run_daily_review_statistics",
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
        "generate_operator_reports",
        "generate_daily_review",
        "generate_daily_review_web",
        "build_ai_review_snapshot",
        "audit_p0_p3_acceptance",
        "audit_p3_candidates",
        "report_real_data_backfill",
        "audit_data_quality",
        "build_empty_table_catalog",
        "assess_data_chains",
        "generate_professional_reports",
        "generate_web_dashboard",
        "generate_trading_terminal",
    ]


def test_operator_backtest_is_optional_until_phase_5_exists():
    steps = command_plan("kpl_data.duckdb")
    optional = {name for name, _, is_optional in steps if is_optional}
    assert optional == {"run_operator_backtest", "run_news_radar"}


def test_integrated_plan_propagates_date_and_prioritizes_capital_flow_collection():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-09",
        include_collection=True,
        max_stocks=12,
        max_sectors=9,
    )
    by_name = {name: cmd for name, cmd, _ in steps}

    assert [steps[0][0], steps[1][0], steps[2][0]] == [
        "collect_market_context",
        "collect_realtime_limit_pool",
        "collect_intraday_stock_flow_market",
    ]
    capital_cmd = by_name["collect_capital_flow_focus"]
    assert capital_cmd[capital_cmd.index("--max-stocks") + 1] == "12"
    assert capital_cmd[capital_cmd.index("--max-sectors") + 1] == "9"
    assert "--strict" in capital_cmd
    multisource_cmd = by_name["collect_multisource_capital_flow"]
    assert multisource_cmd[multisource_cmd.index("--max-stocks") + 1] == "12"
    assert "scripts/run_staged_multisource.py" in multisource_cmd
    assert multisource_cmd[multisource_cmd.index("--stage") + 1] == "close"
    assert by_name["generate_signals"][-2:] == ["--date", "2026-07-09"]
    operator_cmd = by_name["run_daily_operator_loop"]
    assert operator_cmd[operator_cmd.index("--trade-date") + 1] == "2026-07-09"
    assert operator_cmd[operator_cmd.index("--stage") + 1] == "close"


def test_priority_collection_profile_avoids_duplicate_fanout():
    steps = command_plan("sample.duckdb", "2026-07-14", include_collection=True, collection_profile="priority")
    names = [step[0] for step in steps]
    assert names[:5] == [
        "collect_market_context",
        "collect_realtime_limit_pool",
        "collect_intraday_stock_flow_market",
        "collect_intraday_sector_flow_full",
        "collect_finance_gapfill",
    ]
    assert "collect_capital_flow_focus" not in names
    assert "collect_multisource_capital_flow" not in names


def test_auction_plan_retains_blocked_diagnostics():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-24",
        include_collection=True,
        phase="auction",
        collection_profile="priority",
    )
    by_name = {name: cmd for name, cmd, _ in steps}
    names = [name for name, _, _ in steps]

    assert "--allow-blocked" in by_name["generate_auction_stage_signals"]
    assert names.index("generate_auction_stage_signals") < names.index(
        "check_data_readiness"
    )
    assert "generate_web_dashboard" in by_name


def test_intraday_plan_collects_executable_quotes_before_signals():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-30",
        include_collection=True,
        phase="intraday",
        collection_profile="priority",
        signal_limit=120,
    )
    names = [step[0] for step in steps]
    assert "collect_executable_quotes" in names
    assert "collect_l2_focus" in names
    assert names.index("collect_executable_quotes") < names.index(
        "generate_intraday_stage_signals"
    )
    assert names.index("collect_l2_focus") < names.index("generate_intraday_stage_signals")
    assert names.index("run_daily_operator_loop") < names.index(
        "check_data_readiness"
    )
    by_name = {name: cmd for name, cmd, _ in steps}
    quote_cmd = by_name["collect_executable_quotes"]
    assert quote_cmd[quote_cmd.index("--limit") + 1] == "120"
    assert "--auto-boost-if-kpl-stale" in quote_cmd
    assert "--auto-boost-if-kpl-stale" in by_name["collect_l2_focus"]
    assert "generate_trading_terminal" in by_name


def test_close_plan_reuses_intraday_l2_instead_of_fetching_after_hours():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-31",
        include_collection=True,
        phase="close",
        collection_profile="priority",
    )
    names = [step[0] for step in steps]
    assert "collect_l2_focus" not in names
    assert "collect_executable_quotes" in names


def test_close_recovery_as_of_is_applied_to_all_freshness_gates():
    as_of = "2026-07-31T17:45:00"
    steps = command_plan(
        "sample.duckdb",
        "2026-07-31",
        include_collection=False,
        phase="close",
        as_of_time=as_of,
    )
    by_name = {name: command for name, command, _ in steps}
    for name in (
        "generate_close_stage_signals",
        "check_capital_flow_health",
        "check_data_readiness",
        "audit_p0_p3_acceptance",
    ):
        command = by_name[name]
        assert command[command.index("--as-of") + 1] == as_of


def test_close_data_gates_are_deferred_until_reports_but_remain_blocking():
    from scripts.run_integrated_daily import _is_degradable_failure

    for name in (
        "check_data_readiness",
        "check_capital_flow_health",
        "generate_signals",
        "generate_close_stage_signals",
    ):
        assert _is_degradable_failure("close", name) is True
    assert _is_degradable_failure("close", "generate_daily_review") is False


def test_acceptance_audit_is_informational_and_does_not_red_flag_data_run():
    from scripts.run_integrated_daily import (
        DEGRADABLE_EXTERNAL_STEPS,
        INFORMATIONAL_REVIEW_STEPS,
    )

    assert "audit_p0_p3_acceptance" not in DEGRADABLE_EXTERNAL_STEPS
    assert "audit_p0_p3_acceptance" in INFORMATIONAL_REVIEW_STEPS


def test_northbound_is_optional_close_capability():
    from scripts.run_integrated_daily import (
        DEGRADABLE_EXTERNAL_STEPS,
        OPTIONAL_CLOSE_STEPS,
    )

    assert "collect_northbound_daily" in OPTIONAL_CLOSE_STEPS
    assert "collect_northbound_daily" not in DEGRADABLE_EXTERNAL_STEPS


def test_integrated_collection_exits_before_network_on_verified_holiday(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "holiday.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE tushare_trade_cal("
        "exchange VARCHAR, cal_date DATE, is_open BOOLEAN)"
    )
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES (?,?,false)",
        ["SSE", date.today().isoformat()],
    )
    con.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_integrated_daily.py",
            "--db",
            str(db),
            "--trade-date",
            date.today().isoformat(),
            "--phase",
            "close",
        ],
    )
    assert main() == 0
    assert "MARKET_CLOSED" in capsys.readouterr().out


def test_intraday_readiness_blocks_signals_without_killing_retry_watcher():
    assert _is_degradable_failure("intraday", "check_capital_flow_health")
    assert _is_degradable_failure("intraday", "check_data_readiness")
    assert _is_degradable_failure("intraday", "generate_intraday_stage_signals")
    # Close-stage gates are deferred so reports can be generated, but main()
    # returns non-zero after publishing them.
    assert _is_degradable_failure("close", "check_capital_flow_health")
    assert _is_degradable_failure("close", "check_data_readiness")


def test_manifest_upsert_step_transitions_running_to_completed(tmp_path):
    """A5: a step is recorded as 'running' before launch and updated in place to its
    final status, so a single manifest entry reflects progress (no duplicate rows) and
    a hung/killed step stays visible as 'running'."""
    import json
    from trade_system.pipeline_runtime import RunManifest

    m = RunManifest(str(tmp_path), "run-upsert-test", "2026-07-28", "intraday")
    m.upsert_step("step_a", "running", ["python", "a.py"], started_at="t0")
    data = json.loads(m.path.read_text(encoding="utf-8"))
    assert [s["status"] for s in data["steps"]] == ["running"]

    m.upsert_step("step_a", "completed", ["python", "a.py"], return_code=0, duration_seconds=1.5)
    data = json.loads(m.path.read_text(encoding="utf-8"))
    # Single entry transitioned to completed -- not a duplicate running + completed.
    assert len(data["steps"]) == 1
    assert data["steps"][0]["status"] == "completed"
    assert data["steps"][0]["return_code"] == 0
    assert data["steps"][0]["duration_seconds"] == 1.5


def test_manifest_finish_persists_informational_warnings(tmp_path):
    import json
    from trade_system.pipeline_runtime import RunManifest

    m = RunManifest(str(tmp_path), "warning-test", "2026-07-28", "close")
    m.finish("completed", warnings=["operator_readiness_gate_not_passed"])
    data = json.loads(m.path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["warnings"] == ["operator_readiness_gate_not_passed"]


def test_close_tushare_sync_uses_gapfill_lookback():
    """P0#2: close keeps a lookback query but processes only the newest open day.

    Older gaps belong to the explicit history phase; the close budget first
    guarantees today's close snapshot.
    """
    steps = command_plan("sample.duckdb", "2026-07-28", include_collection=True, phase="close")
    by_name = {name: cmd for name, cmd, _ in steps}
    cmd = by_name["sync_tushare_close"]
    assert cmd[cmd.index("--max-days") + 1] == "1"
    assert cmd[cmd.index("--end-date") + 1] == "20260728"
    # The start date precedes the trade date (a lookback window, not a single day).
    assert cmd[cmd.index("--start-date") + 1] < "20260728"


def test_close_plan_chains_isolate_research_and_review():
    """P2-4: research and review steps are categorized for failure isolation; data
    steps (repairs/views/signals) remain fail-fast (in neither chain)."""
    from scripts.run_integrated_daily import (
        RESEARCH_CHAIN_STEPS,
        REVIEW_CHAIN_STEPS,
        command_plan,
    )

    assert "evaluate_qlib_shadow" in RESEARCH_CHAIN_STEPS
    assert "run_strategy_scan" in RESEARCH_CHAIN_STEPS
    assert "reconcile_independent_stock_flow" in RESEARCH_CHAIN_STEPS
    assert "generate_web_dashboard" in REVIEW_CHAIN_STEPS
    assert "generate_daily_review" in REVIEW_CHAIN_STEPS
    # DATA steps are in neither chain (fail-fast).
    assert "repair_critical_integrity" not in RESEARCH_CHAIN_STEPS | REVIEW_CHAIN_STEPS
    assert "build_normalized_views" not in RESEARCH_CHAIN_STEPS | REVIEW_CHAIN_STEPS
    # Close now publishes the operational review without the optional research
    # chain; research remains an explicit compatibility opt-in.
    names = {
        name for name, _, _ in command_plan(
            "sample.duckdb", "2026-07-28", include_collection=True, phase="close")
    }
    assert "evaluate_qlib_shadow" not in names
    assert "build_flow_features" not in names
    assert "build_ai_review_snapshot" not in names
    assert "generate_web_dashboard" in names
    research_names = {
        name for name, _, _ in command_plan(
            "sample.duckdb", "2026-07-28", include_collection=True,
            phase="close", include_research=True)
    }
    assert {"evaluate_qlib_shadow", "build_flow_features", "build_ai_review_snapshot"} <= research_names


def test_close_readiness_gate_is_tightened_to_2h():
    """Audit P2 #1: the close readiness/capital-flow acceptance window is 7200s (2h),
    not the loose 21600s (6h), so a degraded stale intraday snapshot is fail-closed;
    the close-decision SIGNAL evidence window stays at 6h (spans the trading day)."""
    from scripts.run_integrated_daily import CLOSE_READINESS_MAX_AGE_SECONDS, command_plan

    assert CLOSE_READINESS_MAX_AGE_SECONDS == 7200
    steps = command_plan("sample.duckdb", "2026-07-28", include_collection=True, phase="close")
    by_name = {name: cmd for name, cmd, _ in steps}
    for gate in ("check_data_readiness", "check_capital_flow_health"):
        cmd = by_name[gate]
        assert cmd[cmd.index("--max-age-seconds") + 1] == "7200"
    close_sig = by_name["generate_close_stage_signals"]
    assert close_sig[close_sig.index("--freshness-seconds") + 1] == "21600"
