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
    for phase in ('close', 'supplemental'):
        steps = command_plan('sample.duckdb', '2026-09-22', include_collection=True, phase=phase)
        command = next(command for name, command, _ in steps if name == 'collect_auction_market_daily')
        assert command[command.index('--product') + 1] == 'match'


def test_profile_declares_full_market_flow_sources(monkeypatch, capsys):
    import json
    import pytest
    from trade_system.collection_profiles import emit_product_counts, read_product_counts
    context = {'demand_id': 'test-only', 'product_id': 'collect_intraday_stock_flow_market'}
    monkeypatch.setenv('STOCKDATA_REQUEST_CONTEXT', json.dumps(context))
    emit_product_counts('primary', rows_parsed=8, rows_written=0, receipt_reused=True)
    receipt = capsys.readouterr().out
    counted = read_product_counts(receipt, context)
    assert counted['scopes']['primary']['rows_written'] == 0
    assert counted['scopes']['primary']['rows_parsed'] == 8
    assert counted['scopes']['primary']['rows_published'] is None
    assert read_product_counts('old log without instrumentation', context)['status'] == 'unmeasured'
    with pytest.raises(ValueError, match='conflicting'):
        read_product_counts(receipt + receipt, context)
    with pytest.raises(ValueError, match='conflicting'):
        read_product_counts(receipt, {'demand_id': 'other'})
    from trade_system.collection_profiles import product_usage
    usage=product_usage([{'run_id':'synthetic','steps':[{'name':'collect_intraday_stock_flow_market',
        'status':'degraded','rows_written':0,'request_metrics':{'transport_attempts':2}}]}])
    entry=next(p for p in usage['products'] if p['product_id']=='collect_intraday_stock_flow_market')
    assert entry['disposition']=='inspect_failed_input'
    assert entry['observations'][0]['counts']=={'rows_written':0,'rows_parsed':None,'rows_published':None,'transport_attempts':2}
    assert usage['avoidable_cost'] is None and usage['diagnostic_limits']['total']==6
    names = {task.name for task in phase_tasks("intraday")}
    assert {"collect_intraday_stock_flow_market", "collect_intraday_sector_flow_full"} <= names
    from trade_system.collection_profiles import validate_plan
    for phase in ('auction','intraday','close','supplemental','history'):
        plan = command_plan('unused', '2026-09-18', include_collection=True, phase=phase)
        assert [t.name for t in phase_tasks(phase)] == [n for n,_,_ in plan]
    supplement = command_plan('unused', '2026-09-18', include_collection=True, phase='supplemental')
    name, command, _ = next(step for step in supplement if step[0]=='sync_tushare_close')
    assert name == 'sync_tushare_close'
    assert command[command.index('--start-date') + 1] == '20260918'
    assert command[command.index('--end-date') + 1] == '20260918'
    assert command[command.index('--retry-passes') + 1] == '0'
    with pytest.raises(ValueError, match='unregistered'):
        task_due('unused','2026-09-18','unknown',phase='close',force=True)
    with pytest.raises(ValueError, match='unregistered'):
        validate_plan([('unknown',[],False)])


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
    assert not {"collect_capital_flow_focus", "collect_multisource_capital_flow"} & set(names)

    assert names[:9] == [
        "collect_market_context",
        "sync_tushare_close",
        "collect_ths_concepts_api",
        "collect_hithink_limit_pool_daily",
        "collect_realtime_limit_pool",
        "collect_kpl_stock_flow_focus",
        "collect_intraday_stock_flow_market",
        "collect_intraday_sector_flow_full",
        "derive_market_context",
    ]


def test_close_tushare_checkpoint_requires_all_five_successful_datasets(tmp_path, monkeypatch):
    db = tmp_path / "tushare-close.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE history_fetch_checkpoint("
        "dataset VARCHAR, trade_date DATE, page_no INTEGER, status VARCHAR, "
        "rows_written INTEGER, updated_at TIMESTAMP)"
    )
    for dataset in ("daily", "daily_basic", "adj_factor", "moneyflow", "industry_flow"):
        con.execute(
            "INSERT INTO history_fetch_checkpoint VALUES (?, '2026-07-15', 0, 'success', 10, '2026-07-15 19:01:00')",
            [dataset],
        )
    con.close()

    now = datetime(2026, 7, 15, 19, 10)
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
        now=datetime(2026, 7, 15, 19, 32),
    )[0] is True
    from trade_system.collection_profiles import close_datasets
    assert close_datasets('2026-07-15', '2026-07-15T17:30:00+08:00') == ('daily','daily_basic','adj_factor')
    assert len(close_datasets('2026-07-15', '2026-07-15T11:00:00+00:00')) == 5
    assert task_due(db, '2026-07-15', 'reconcile_independent_stock_flow', phase='close',
                    now=datetime(2026,7,15,17,30))[1].startswith('publication pending:')
    early = dict((name, command) for name,command,_ in command_plan(str(db),'2026-07-15',
                 phase='close',include_collection=True,as_of_time='2026-07-15T17:30:00'))
    assert early['sync_tushare_close'][early['sync_tushare_close'].index('--datasets')+1] == 'daily,daily_basic,adj_factor'
    assert early['check_capital_flow_health'][early['check_capital_flow_health'].index('--stage')+1] == 'intraday'
    from collectors import xiaodefa
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)")
        con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2026-07-14',true),('SSE','2026-07-15',true)")
    class Clock(datetime):
        @classmethod
        def now(cls): return cls(2026,7,15,20)
    monkeypatch.setattr(xiaodefa,'datetime',Clock)
    monkeypatch.setattr(xiaodefa,'XiaodefaClient',lambda:object())
    calls=[]
    monkeypatch.setitem(xiaodefa.COLLECTORS,'margin',dict(table='fixture',replace_on=(),
        fetch=lambda client,args:calls.append(args.trade_date) or [{'trade_date':args.trade_date}]))
    monkeypatch.setattr(xiaodefa,'store_rows',lambda store,table,rows,keys,**k:len(rows))
    args=dict(db_path=str(db),trade_date='2026-07-15',start_date='2026-07-15',end_date='2026-07-15')
    waiting=xiaodefa.collect(['margin'],**args)
    assert waiting['margin']['status']=='awaiting_publication' and not calls
    disclosed=xiaodefa.collect(['margin'],latest_disclosed=True,**args)
    assert calls==['2026-07-14']
    assert disclosed['margin']['requested_date']=='2026-07-15' and disclosed['margin']['source_date']=='2026-07-14'
    monkeypatch.setitem(xiaodefa.COLLECTORS['margin'],'fetch',lambda *a:[{'trade_date':'2026-07-15'}])
    assert xiaodefa.collect(['margin'],latest_disclosed=True,**args)['margin']['status']=='error'
    with duckdb.connect(str(db)) as con:
        con.execute('DROP TABLE tushare_trade_cal')
    # An unrelated successful supplement cannot satisfy publication inputs.
    from trade_system.collection_profiles import publication_readiness
    result = publication_readiness(db, '2026-07-15')
    assert not result['passed'] and result['reason'] == 'publication_inputs_unavailable'
    import hashlib
    import json
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)')
        con.execute('CREATE TABLE tushare_stock_basic(ts_code VARCHAR,stock_code VARCHAR,stock_name VARCHAR,area VARCHAR,industry VARCHAR,market VARCHAR,list_date DATE,delist_date DATE)')
        con.execute("INSERT INTO tushare_stock_basic VALUES ('000001.SZ','000001','sample','','','','1991-04-03',NULL)")
        con.execute('CREATE TABLE multi_source_observation(data_type VARCHAR,provider VARCHAR,status VARCHAR,payload_json VARCHAR,observed_at TIMESTAMP,payload_hash VARCHAR)')
        ref = con.execute('SELECT * FROM tushare_stock_basic ORDER BY ts_code').fetchall()
        version = hashlib.sha256(json.dumps(ref,ensure_ascii=False,default=str,separators=(',', ':')).encode()).hexdigest()
        payload = json.dumps({'version':version,'scope':['L','D']})
        con.execute("INSERT INTO multi_source_observation VALUES ('tushare_stock_basic_snapshot','xiaodefa','qualified',?,current_timestamp,?)",
                    [payload,hashlib.sha256(payload.encode()).hexdigest()])
        con.execute("CREATE TABLE v_kline_daily AS SELECT '2026-07-15' AS trade_date, '000001' AS stock_code, 10.0 AS close, 1.0 AS change_pct, 'xiaodefa' AS provider, 'none' AS adjustment, 'hands' AS volume_unit, 'thousand_yuan' AS amount_unit")
        con.execute("UPDATE history_fetch_checkpoint SET rows_written=1 WHERE dataset='daily'")
    assert publication_readiness(db, '2026-07-15')['passed']
    with duckdb.connect(str(db)) as con:
        con.execute('INSERT INTO v_kline_daily SELECT * FROM v_kline_daily')
    assert publication_readiness(db, '2026-07-15')['reason'] == 'canonical_prices_incomplete'
    with duckdb.connect(str(db)) as con:
        con.execute("UPDATE tushare_stock_basic SET list_date='1970-01-01'")
    assert publication_readiness(db, '2026-07-15')['reason'] == 'stock_reference_unqualified'


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
    )[0] is True
    with duckdb.connect(str(db)) as con:
        con.execute("INSERT INTO daily_summary VALUES ('2026-07-15','2026-07-15 10:59:30','real')")
    assert not task_due(db, '2026-07-15', 'collect_market_context', now=datetime(2026,7,15,11))[0]



def test_task_due_is_phase_aware_for_shared_tasks(tmp_path):
    """Audit P2 #2: a shared task name must use the active phase's cadence.  A
    ~2000s-old market-context snapshot is fresh under the close 3600s TTL but due
    under the auction 300s TTL (a phase-blind first-match would always use 300s)."""
    from datetime import timedelta

    db = tmp_path / "phase-aware.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE daily_summary (date DATE, source_kind VARCHAR, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE market_rise_fall(date DATE,source_kind VARCHAR,updated_at TIMESTAMP)")
    fetched = datetime(2026, 7, 15, 17, 0, 0)
    con.execute(
        "INSERT INTO daily_summary VALUES (DATE '2026-07-15', 'real', ?)", [fetched])
    con.close()

    with duckdb.connect(str(db)) as con:
        con.execute("INSERT INTO market_rise_fall VALUES ('2026-07-15','real',?)", [fetched])
    now = fetched + timedelta(seconds=2000)  # 2000s old
    due_close, reason_close = task_due(
        str(db), "2026-07-15", "collect_market_context", phase="close", now=now)
    assert due_close is False, reason_close  # 2000s < 3600s close TTL -> fresh
    due_auction, reason_auction = task_due(
        str(db), "2026-07-15", "collect_market_context", phase="auction", now=now)
    assert due_auction is True, reason_auction  # 2000s > 300s auction TTL -> due


def test_history_uses_single_plan_and_canaries_are_explicit_only(monkeypatch):
    from trade_system.collection_profiles import HISTORY_SUPPLEMENT_TYPES
    for phase in ("auction", "intraday", "close", "history"):
        plan = command_plan("sample.duckdb", "2026-07-15", include_collection=True, phase=phase)
        commands = [part for _, command, _ in plan for part in command]
        assert "scripts/run_staged_multisource.py" not in commands
        assert "scripts/check_kpl_connectivity.py" not in commands
        if phase == "history":
            command = next(command for name, command, _ in plan if name == "collect_history_supplement")
            assert command[1] == "scripts/collect_multisource.py"
            assert command[command.index("--types") + 1] == ",".join(HISTORY_SUPPLEMENT_TYPES)
            assert "--resume" in command

    # Explicit historical samples cannot include future watchlist rows or repeat a code.
    from scripts import collect_minute_snapshots as minute
    monkeypatch.setattr(minute, '_market_of', lambda code: 'SZ')
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE watchlist(trade_date DATE,stock_code VARCHAR)')
        con.execute("INSERT INTO watchlist VALUES ('2026-09-17','000001'),('2026-09-18','000002'),('2026-09-19','000003')")
        assert minute._universe(con,'2026-09-18','watchlist',10)==['000002']
        assert minute._universe(con,'2026-09-18','000002,000002, 000001',10)==['000001','000002']
