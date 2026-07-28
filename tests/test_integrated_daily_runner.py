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
        "migrate_stock_flow_contract",
        "repair_critical_integrity",
        "repair_kline_raw_json",
        "migrate_index_kline_date",
        "derive_index_kline",
        "repair_p2_reference",
        "repair_duplicates",
        "build_normalized_views",
        "ensure_operational_indexes",
        "audit_multisource_readiness",
        "audit_stock_flow_contract",
        "build_operator_views",
        "build_auction_evidence",
        "check_data_readiness",
        "check_capital_flow_health",
        "generate_signals",
        "generate_close_stage_signals",
        "create_operator_outcome_template",
        "run_daily_operator_loop",
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
        "evaluate_qlib_shadow",
        "generate_operator_reports",
        "generate_daily_review",
        "audit_p0_p3_acceptance",
        "audit_p3_candidates",
        "report_real_data_backfill",
        "audit_data_quality",
        "build_empty_table_catalog",
        "assess_data_chains",
        "generate_professional_reports",
        "generate_web_dashboard",
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
        "migrate_stock_flow_contract",
        "collect_market_context",
        "collect_realtime_limit_pool",
    ]
    assert steps[3][0] == "collect_intraday_stock_flow_market"
    capital_cmd = by_name["collect_capital_flow_focus"]
    assert capital_cmd[capital_cmd.index("--max-stocks") + 1] == "12"
    assert capital_cmd[capital_cmd.index("--max-sectors") + 1] == "9"
    assert "--strict" in capital_cmd
    multisource_cmd = by_name["collect_multisource_capital_flow"]
    assert multisource_cmd[multisource_cmd.index("--max-stocks") + 1] == "12"
    assert "scripts/run_staged_multisource.py" in multisource_cmd
    assert multisource_cmd[multisource_cmd.index("--stage") + 1] == "close"
    assert by_name["generate_signals"][-2:] == ["--date", "2026-07-09"]
    assert by_name["run_daily_operator_loop"][-2:] == ["--trade-date", "2026-07-09"]


def test_priority_collection_profile_avoids_duplicate_fanout():
    steps = command_plan("sample.duckdb", "2026-07-14", include_collection=True, collection_profile="priority")
    names = [step[0] for step in steps]
    assert names[:5] == [
        "migrate_stock_flow_contract",
        "collect_market_context",
        "collect_realtime_limit_pool",
        "collect_intraday_stock_flow_market",
        "collect_intraday_sector_flow_full",
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

    assert "--allow-blocked" in by_name["generate_auction_stage_signals"]
    assert "generate_web_dashboard" in by_name


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


def test_close_tushare_sync_uses_gapfill_lookback():
    """P0#2: the close-phase TuShare sync scans a lookback window with --max-days 0 so
    recent missing sessions are backfilled (the history checkpoint skips dates that
    are already synced)."""
    steps = command_plan("sample.duckdb", "2026-07-28", include_collection=True, phase="close")
    by_name = {name: cmd for name, cmd, _ in steps}
    cmd = by_name["sync_tushare_close"]
    assert cmd[cmd.index("--max-days") + 1] == "0"
    assert cmd[cmd.index("--end-date") + 1] == "20260728"
    # The start date precedes the trade date (a lookback window, not a single day).
    assert cmd[cmd.index("--start-date") + 1] < "20260728"
