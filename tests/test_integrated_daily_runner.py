from datetime import date
import sys

import duckdb
import pytest

from scripts.run_integrated_daily import (
    _decode_process_bytes,
    _utf8_subprocess_env,
    command_plan,
    main,
)


def test_integrated_plan_propagates_date_and_prioritizes_capital_flow_collection():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-09",
        include_collection=True,
        phase="close",
    )
    by_name = {name: cmd for name, cmd, _ in steps}

    assert [steps[0][0], steps[1][0], steps[2][0]] == [
        "collect_market_context",
        "check_kpl_connectivity",
        "sync_tushare_close",
    ]
    assert "collect_capital_flow_focus" not in by_name
    assert "collect_multisource_capital_flow" not in by_name
    assert 'generate_signals' not in by_name
    assert 'run_daily_operator_loop' not in by_name


def test_priority_collection_profile_avoids_duplicate_fanout():
    steps = command_plan("sample.duckdb", "2026-07-14", include_collection=True, collection_profile="priority")
    names = [step[0] for step in steps]
    assert names[:5] == [
        "collect_market_context",
        "check_kpl_connectivity",
        "sync_tushare_close",
        "sync_tushare_ohlc_core",
        "collect_ths_concepts_api",
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

    assert 'generate_auction_stage_signals' not in by_name
    assert names.index('collect_realtime_limit_pool') < names.index('check_data_readiness')
    assert "generate_web_dashboard" not in by_name


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
    assert names.index('collect_executable_quotes') < names.index('check_data_readiness')
    assert names.index('collect_l2_focus') < names.index('check_data_readiness')
    assert not {'generate_intraday_stage_signals','run_daily_operator_loop'} & set(names)
    by_name = {name: cmd for name, cmd, _ in steps}
    quote_cmd = by_name["collect_executable_quotes"]
    assert quote_cmd[quote_cmd.index("--limit") + 1] == "120"
    assert "--auto-boost-if-kpl-stale" in quote_cmd
    assert "--auto-boost-if-kpl-stale" in by_name["collect_l2_focus"]
    assert "generate_trading_terminal" not in by_name


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
    assert names.index("collect_ths_concepts_api") < names.index(
        "collect_intraday_sector_flow_full"
    )


def test_subprocess_text_protocol_is_utf8_and_never_injects_replacement_character():
    env = _utf8_subprocess_env()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    text, broken = _decode_process_bytes("启动".encode("utf-8"), "stdout")
    assert (text, broken) == ("启动", False)
    text, broken = _decode_process_bytes("启动".encode("gbk"), "stdout")
    assert broken is True
    assert "�" not in text
    assert "stdout_encoding_error" in text


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
        "check_capital_flow_health",
        "check_data_readiness",
    ):
        command = by_name[name]
        assert command[command.index("--as-of") + 1] == as_of


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
    from tools.v2.backup_verify import backup_verify
    migration_root=tmp_path/'migration'
    verified=backup_verify(db,migration_root)
    db=verified['backup']
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_integrated_daily.py",
            "--migration-root",
            str(migration_root),
            "--reports-dir",
            str(migration_root/'reports'),
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
    assert 'generate_close_stage_signals' not in by_name



@pytest.mark.parametrize("flag", ["--report-only", "--render-only", "--include-research"])
def test_retired_cli_flags_fail_before_database_access(tmp_path, monkeypatch, flag):
    db = tmp_path / "must-not-create.duckdb"
    monkeypatch.setattr(sys, "argv", ["run_integrated_daily.py", "--db", str(db), flag])
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 2 and not db.exists()


def test_all_collection_phases_keep_data_and_retire_user_publications():
    retired = {"generate_web_dashboard", "generate_daily_review", "generate_daily_review_web",
               "generate_trading_terminal", "run_strategy_scan", "run_stage_backtest",
               "run_qlib_daily", "build_flow_features", "build_ai_review_snapshot",
               "generate_operator_reports", "create_operator_outcome_template"}
    for phase in ("auction", "intraday", "close", "supplemental", "history"):
        names = {n for n, _, _ in command_plan("sample.duckdb", "2026-09-16", include_collection=True, phase=phase)}
        assert not names & retired
    with pytest.raises(ValueError, match="research retired"):
        command_plan("sample.duckdb", "2026-09-16", include_research=True)


def test_supplemental_keeps_four_collectors_under_shared_plan():
    from trade_system.collection_profiles import task_due, resolve_phase
    from trade_system.source_authority import validate_production_plan
    steps=command_plan('sample.duckdb','2026-09-17',include_collection=True,phase='supplemental')
    names=[n for n,_,_ in steps]
    assert names==['collect_lhb_daily','collect_auction_market_daily','collect_index_kline_daily',
                   'collect_xiaodefa_critical','build_normalized_views']
    validate_production_plan('supplemental',names)
    with pytest.raises(ValueError):validate_production_plan('supplemental',names[1:])
    assert resolve_phase('supplemental')=='supplemental'
    for name in names[:4]:
        assert task_due('must-not-open.duckdb','2026-09-17',name,phase='supplemental')[0]


def test_collection_handover_binds_sources_runtime_and_exact_targets(tmp_path, monkeypatch):
    import hashlib
    import json
    import subprocess
    from trade_system.source_authority import collection_contract, verify_collection_contract
    source=tmp_path/'source';source.mkdir()
    (source/'fetch_all.py').write_text('# synthetic collector')
    cache=source/'trade_system/.stock_cache/limiter.json';cache.parent.mkdir(parents=True)
    cache.write_text('{"attempts":1}')
    thresholds=source/'config/phase_thresholds.json';thresholds.parent.mkdir()
    thresholds.write_text('{"threshold":1}')
    db=tmp_path/'market.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(cal_date DATE)')
        con.execute('CREATE TABLE v_kline_daily(trade_date DATE)')
    def runtime(*args,**kwargs):
        return subprocess.CompletedProcess(args,0,stdout='[[3,12,4],[["fixture","1"]]]')
    monkeypatch.setattr(subprocess,'run',runtime)
    output=tmp_path/'data-reports'
    manifest=collection_contract(source,db,output,sys.executable)
    path=tmp_path/'contract.json';path.write_text(json.dumps(manifest),encoding='utf-8')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_collection_contract(path,digest,db,output)==manifest
    cache.write_text('{"attempts":2}')
    assert verify_collection_contract(path,digest,db,output)==manifest
    assert 'trade_system/.stock_cache/limiter.json' not in manifest['files']
    thresholds.write_text('{"threshold":2}')
    with pytest.raises(ValueError,match='source/runtime changed'):
        verify_collection_contract(path,digest,db,output)
    thresholds.write_text('{"threshold":1}')
    with pytest.raises(ValueError,match='target changed'):
        verify_collection_contract(path,digest,db,tmp_path/'different')
    with pytest.raises(ValueError,match='supplied hash'):
        verify_collection_contract(path,'0'*64,db,output)
    (source/'fetch_all.py').write_text('# changed fixture')
    with pytest.raises(ValueError,match='source/runtime changed'):
        verify_collection_contract(path,digest,db,output)
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE account_snapshot(id INT)')
    with pytest.raises(ValueError,match='V2/account'):
        collection_contract(source,db,output,sys.executable)
