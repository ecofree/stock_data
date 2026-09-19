import duckdb

from trade_system.daily_review import build_daily_review_context, render_daily_review_markdown
from trade_system.operator_outcomes import init_trading_tables


def test_daily_review_report_contains_operator_required_sections(tmp_path):
    db_path = tmp_path / "daily_review.duckdb"
    init_trading_tables(db_path)
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE market_regime_snapshot("
        "trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, evidence_json VARCHAR)"
    )
    con.execute("INSERT INTO market_regime_snapshot VALUES ('2026-07-06','weak',20,5,'{}')")
    con.execute(
        "CREATE TABLE sector_rotation_score("
        "trade_date VARCHAR, sector_code VARCHAR, sector_name VARCHAR, score DOUBLE, strength_value DOUBLE, "
        "limit_up_count INTEGER, seal_rate DOUBLE, evidence_json VARCHAR)"
    )
    con.execute("INSERT INTO sector_rotation_score VALUES ('2026-07-06','801001','Theme',72,80,6,60,'{}')")
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, decision VARCHAR, evidence_json VARCHAR)"
    )
    con.execute("INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-06','premarket_pool','000001','Alpha',80,'pool','{}')")
    con.execute("CREATE TABLE alert_events(trade_date VARCHAR, severity VARCHAR, category VARCHAR, message VARCHAR, evidence_json VARCHAR)")
    con.execute("INSERT INTO alert_events VALUES ('2026-07-06','P1','risk','reduce risk','{}')")
    con.execute(
        "INSERT INTO watchlist(trade_date, stock_code, stock_name, sector_code, thesis, invalidation, priority, status) "
        "VALUES ('2026-07-06','000001','Alpha','801001','leader','break board',1,'active')"
    )
    con.execute(
        "INSERT INTO trade_plan(trade_date, stock_code, stock_name, setup_type, entry_condition, stop_condition, target_condition, max_position_pct, status) "
        "VALUES ('2026-07-06','000001','Alpha','manual','confirm','break','review',5,'planned')"
    )
    con.execute(
        "INSERT INTO risk_snapshot(trade_date,total_position_pct,max_single_position_pct,max_sector_position_pct,daily_loss_limit_pct,current_drawdown_pct,risk_state,evidence_json) "
        "VALUES ('2026-07-06',0,5,5,2,0,'defensive','{}')"
    )
    con.execute(
        "INSERT INTO trade_journal(trade_date, stock_code, stock_name, action, action_time, price, position_pct, reason, mistake_tag) "
        "VALUES ('2026-07-06','000001','Alpha','close_decision','close',NULL,0,'decision=reduce','pending_review')"
    )
    con.close()

    context = build_daily_review_context(db_path, "2026-07-06")
    report = render_daily_review_markdown(context)

    assert context["trade_date"] == "2026-07-06"
    for section in [
        "今日裁决",
        "Market Regime",
        "Capital Flow Coverage",
        "Individual Stock Main-Net Inflow Top 50",
        "Individual Stock Main-Net Outflow Top 50",
        "THS Concept Flow Inflow Top 10",
        "THS Concept Flow Outflow Top 10",
        "THS Concepts With Limit-Up Stocks",
        "Potential Stocks (Research Only)",
        "Mainline Themes",
        "Four-Stage Candidates",
        "Risk Alerts",
            "Research Plan Drafts (Execution Gate Closed)",
        "Mistakes And Invalidations",
        "Next-Day Focus",
        "Data Gaps And Degradation",
    ]:
        assert section in report
    assert "weak" in report
    assert "pending_review" in report


def test_review_queries_distinguish_empty_missing_and_failed():
    from trade_system.review_queries import _rows, _query_status
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE present(value INTEGER)")
    outcomes = []
    token = _query_status.set(outcomes)
    try:
        for table, sql in [("missing", "SELECT * FROM missing"), ("present", "SELECT * FROM present"),
                           ("present", "SELECT nonexistent FROM present")]:
            assert _rows(con,table,sql,[]) == []
    finally:
        _query_status.reset(token)
        con.close()
    assert [r["state"] for r in outcomes] == ["missing_table","empty","query_failed"]


def test_review_date_uses_calendar_not_old_signal_and_never_future():
    import pytest
    from trade_system.review_queries import _latest_date
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE stock_candidate_score(trade_date DATE)")
        con.execute("INSERT INTO stock_candidate_score VALUES ('2099-01-01')")
        with pytest.raises(ValueError,match="calendar"):
            _latest_date(con,"2026-09-18")
        con.execute("CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)")
        con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2026-09-18',true),('SSE','2026-09-21',true)")
        assert _latest_date(con,"2026-09-18") == "2026-09-18"
        con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2026-09-18',false)")
        with pytest.raises(ValueError,match="contradictory"):
            _latest_date(con,"2026-09-18")


def test_query_failure_blocks_every_review_state_even_when_old_readiness_is_green(tmp_path, monkeypatch):
    from trade_system import daily_review
    db = tmp_path/"bad_schema.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE watchlist(stock_code VARCHAR)")
    monkeypatch.setattr(daily_review,"assess_trade_date_readiness",lambda *a,**kw: {
        "source_ready":True,"pipeline_ready":True,"artifact_current":True,
        "data_certified_ready":True,"execution_ready":True,"missing_groups":[]})
    ctx=daily_review.build_daily_review_context(db,"2026-09-18")
    assert any(row["table"]=="watchlist" and row["state"]=="query_failed" for row in ctx["query_status"])
    for state in (ctx["readiness"],ctx["readiness"]["operator_state"],ctx["execution_control"]):
        assert state["operator_status"]=="blocked" and not state["execution_ready"] and not state["analysis_ready"]
    assert ctx["execution_control"]["effective_position_pct"]==0
