from datetime import datetime

import duckdb

from scripts.run_integrated_daily import command_plan
from trade_system.collection_profiles import phase_tasks, resolve_phase, task_due


def test_candidate_audit_uses_qualified_prior_membership_and_fails_on_missing_snapshot(tmp_path):
    from scripts.audit_p3_candidates import audit

    db = tmp_path / "p3.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE sector_rotation_score(trade_date DATE,sector_code VARCHAR,sector_name VARCHAR,score DOUBLE)")
        con.execute("CREATE TABLE v_sector_capital(trade_date DATE,sector_code VARCHAR,sector_name VARCHAR,sector_type VARCHAR,main_net_inflow DOUBLE)")
        con.execute("INSERT INTO v_sector_capital VALUES ('2026-09-16','THS-A','A','ths_concept_derived',10),('2026-09-16','THS-X','unknown','ths_concept_derived',20),('2026-09-16','EM-A','industry','em_industry',30)")
        con.execute("CREATE TABLE v_default_concept_daily(trade_date DATE,concept_code VARCHAR)")
        con.execute("INSERT INTO v_default_concept_daily VALUES ('2026-09-15','THS-A'),('2026-09-17','THS-X')")
        con.execute("CREATE TABLE v_default_concept_stock_history AS SELECT * FROM v_default_concept_daily")
    result = audit(str(db), "2026-09-16", str(tmp_path / "available.md"))
    assert result["membership_snapshot"] == "2026-09-15"
    assert result["taxonomy_counts"] == {"concept": 1, "industry": 1}
    assert result["status"] == "pass" and result["membership_age_days"] == 1
    result = audit(str(db), "2026-09-25", str(tmp_path / "stale.md"))
    assert result["status"] == "fail" and result["membership_status"] == "stale"
    with duckdb.connect(str(db)) as con:
        con.execute("DROP TABLE v_default_concept_stock_history")
    assert audit(str(db), "2026-09-16", str(tmp_path / "missing.md"))["status"] == "fail"


def test_phase_auto_resolves_market_windows():
    assert resolve_phase("auto", datetime(2026, 7, 15, 9, 0)) == "auction"
    assert resolve_phase("auto", datetime(2026, 7, 15, 10, 0)) == "intraday"
    assert resolve_phase("auto", datetime(2026, 7, 15, 16, 0)) == "close"
    assert resolve_phase("auto", datetime(2026, 7, 15, 22, 0)) == "close"


def test_retired_full_phase_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        resolve_phase("full")


def test_intraday_plan_excludes_after_close_fanout():
    steps = command_plan("sample.duckdb", "2026-07-15", include_collection=True, phase="intraday")
    names = [name for name, _, _ in steps]
    assert names == [
        "collect_market_context",
        "check_kpl_connectivity",
        "collect_realtime_limit_pool",
        "collect_intraday_stock_flow_market",
        "collect_l2_focus",
        "collect_intraday_sector_flow_full",
        "derive_market_context",
        "build_normalized_views",
        "collect_executable_quotes",
        "audit_multisource_readiness",
        "check_capital_flow_health",
        "check_data_readiness",
    ]
    assert "collect_finance_gapfill" not in names
    assert "evaluate_qlib_shadow" not in names
    assert "run_news_radar" not in names
    assert not {'generate_signals','generate_intraday_stage_signals','run_daily_operator_loop'} & set(names)


def test_migration_plans_do_not_call_retired_decision_or_terminal_entries():
    retired = {'generate_signals.py', 'generate_stage_signals.py',
               'run_daily_operator_loop.py', 'generate_trading_terminal.py',
               'repair_critical_integrity.py'}
    for phase in ('auction', 'intraday', 'close', 'history'):
        steps = command_plan("sample.duckdb", "2026-07-15", include_collection=True, phase=phase)
        assert not {arg.removeprefix('scripts/') for _, command, _ in steps for arg in command} & retired


def test_profile_declares_full_market_flow_sources():
    names = {task.name for task in phase_tasks("intraday")}
    assert {"collect_intraday_stock_flow_market", "collect_intraday_sector_flow_full"} <= names


def test_fresh_intraday_snapshots_are_not_due(tmp_path):
    db = tmp_path / "fresh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE realtime_candidate_pool_snapshot(trade_date DATE, fetched_at TIMESTAMP, status VARCHAR)")
    con.execute("CREATE TABLE intraday_stock_flow_batch(trade_date DATE, updated_at TIMESTAMP, status VARCHAR)")
    con.execute("CREATE TABLE intraday_sector_flow_batch(trade_date DATE, updated_at TIMESTAMP, status VARCHAR)")
    con.execute("INSERT INTO realtime_candidate_pool_snapshot VALUES ('2026-07-15', '2026-07-15 10:59:00', 'success')")
    con.execute("INSERT INTO intraday_stock_flow_batch VALUES ('2026-07-15', '2026-07-15 10:57:00', 'success')")
    con.execute("INSERT INTO intraday_sector_flow_batch VALUES ('2026-07-15', '2026-07-15 10:58:00', 'partial')")
    con.close()
    now = datetime(2026, 7, 15, 11, 0)
    assert task_due(db, "2026-07-15", "collect_realtime_limit_pool", now=now)[0] is False
    assert task_due(db, "2026-07-15", "collect_intraday_stock_flow_market", now=now)[0] is False
    # Partial sector coverage has a shorter retry cadence (300 seconds).
    assert task_due(db, "2026-07-15", "collect_intraday_sector_flow_full", now=now)[0] is False


def test_close_priority_plan_keeps_incremental_tushare_and_official_ths():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-15",
        include_collection=True,
        phase="close",
        collection_profile="priority",
    )
    names = [name for name, _, _ in steps]

    assert names[:11] == [
        "collect_market_context",
        "check_kpl_connectivity",
        "sync_tushare_close",
        "sync_tushare_ohlc_core",
        "collect_ths_concepts_api",
        "collect_hithink_limit_pool_daily",
        "collect_realtime_limit_pool",
        "collect_kpl_stock_flow_focus",
        "collect_intraday_stock_flow_market",
        "collect_intraday_sector_flow_full",
        "derive_market_context",
    ]


def test_close_tushare_checkpoint_requires_all_five_successful_datasets(tmp_path):
    db = tmp_path / "tushare-close.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE history_fetch_checkpoint("
        "dataset VARCHAR, trade_date DATE, page_no INTEGER, status VARCHAR, "
        "rows_written INTEGER, updated_at TIMESTAMP)"
    )
    for dataset in ("daily", "daily_basic", "adj_factor", "moneyflow", "industry_flow"):
        con.execute(
            "INSERT INTO history_fetch_checkpoint VALUES (?, '2026-07-15', 0, 'success', 10, '2026-07-15 17:31:00')",
            [dataset],
        )
    con.close()

    now = datetime(2026, 7, 15, 17, 40)
    assert task_due(db, "2026-07-15", "sync_tushare_close", now=now)[0] is False

    con = duckdb.connect(str(db))
    con.execute(
        "UPDATE history_fetch_checkpoint SET status='failed', rows_written=0 "
        "WHERE dataset='moneyflow'"
    )
    con.close()
    due, reason = task_due(db, "2026-07-15", "sync_tushare_close", now=now)
    assert due is False
    assert "status=partial" in reason
    # Failed/partial checkpoints use a shorter 30-minute TTL.
    assert task_due(
        db, "2026-07-15", "sync_tushare_close",
        now=datetime(2026, 7, 15, 18, 2),
    )[0] is True


def test_legacy_weekly_web_refresh_is_not_a_close_task():
    # The official HiThink collector is now the sole close-path concept
    # producer.  The old web refresh remains available only as explicit
    # historical recovery, so its failed checkpoint cannot suppress a close
    # run or compete with the official snapshot.
    assert not any(
        task.name == "refresh_ths_weekly"
        for task in phase_tasks("close")
    )


def test_market_context_fallback_never_suppresses_real_retry(tmp_path):
    db = tmp_path / "fallback-retry.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, fetched_at TIMESTAMP, source_kind VARCHAR)"
    )
    con.execute(
        "CREATE TABLE market_rise_fall("
        "date DATE, updated_at TIMESTAMP, source_kind VARCHAR)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES "
        "('2026-07-15','2026-07-15 10:59:00','fallback')"
    )
    con.execute(
        "INSERT INTO market_rise_fall VALUES "
        "('2026-07-15','2026-07-15 10:59:00','fallback')"
    )
    con.close()

    due, reason = task_due(
        db,
        "2026-07-15",
        "collect_market_context",
        now=datetime(2026, 7, 15, 11, 0),
    )
    assert due is True
    assert reason == "no same-date snapshot"

    con = duckdb.connect(str(db))
    con.execute(
        "INSERT INTO market_rise_fall VALUES "
        "('2026-07-15','2026-07-15 10:59:30','real')"
    )
    con.close()
    assert task_due(
        db,
        "2026-07-15",
        "collect_market_context",
        now=datetime(2026, 7, 15, 11, 0),
    )[0] is False


def test_task_due_is_phase_aware_for_shared_tasks(tmp_path):
    """Audit P2 #2: a shared task name must use the active phase's cadence.  A
    ~2000s-old market-context snapshot is fresh under the close 3600s TTL but due
    under the auction 300s TTL (a phase-blind first-match would always use 300s)."""
    from datetime import timedelta

    db = tmp_path / "phase-aware.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE daily_summary (date DATE, source_kind VARCHAR, fetched_at TIMESTAMP)")
    fetched = datetime(2026, 7, 15, 17, 0, 0)
    con.execute(
        "INSERT INTO daily_summary VALUES (DATE '2026-07-15', 'real', ?)", [fetched])
    con.close()

    now = fetched + timedelta(seconds=2000)  # 2000s old
    due_close, reason_close = task_due(
        str(db), "2026-07-15", "collect_market_context", phase="close", now=now)
    assert due_close is False, reason_close  # 2000s < 3600s close TTL -> fresh
    due_auction, reason_auction = task_due(
        str(db), "2026-07-15", "collect_market_context", phase="auction", now=now)
    assert due_auction is True, reason_auction  # 2000s > 300s auction TTL -> due
