import duckdb

from trade_system.reports.operator_report import (
    build_operator_report_snapshot,
    persist_operator_report_snapshot,
    render_operator_report_markdown,
)


def test_operator_report_snapshot_collects_professional_sections(tmp_path):
    db_path = tmp_path / "operator_report.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE TABLE data_source_catalog(source_id VARCHAR, data_domain VARCHAR, enabled BOOLEAN)")
        con.execute("INSERT INTO data_source_catalog VALUES ('kpl_api','auction',true), ('qlib_shadow','ml_shadow',false)")
        con.execute(
            """
            CREATE TABLE strategy_scan_result(
                trade_date VARCHAR, strategy_id VARCHAR, symbol VARCHAR, stock_name VARCHAR,
                stage VARCHAR, score DOUBLE, evidence_json VARCHAR, selected_reason VARCHAR,
                risk_points VARCHAR, invalid_conditions VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO strategy_scan_result VALUES "
            "('2026-07-07','stage.pre','000001','Test Stock','pre_market',80,'{}',"
            "'theme strong','weak market downgrade','auction not confirmed')"
        )
        con.execute(
            "CREATE TABLE strategy_backtest_result("
            "strategy_id VARCHAR, stage VARCHAR, sample_count INTEGER, win_rate DOUBLE, avg_return DOUBLE)"
        )
        con.execute("INSERT INTO strategy_backtest_result VALUES ('stage.pre','pre_market',10,60.0,1.5)")
        con.execute("CREATE TABLE news_radar_item(news_id VARCHAR, trade_date VARCHAR, related_sector VARCHAR, title VARCHAR)")
        con.execute("INSERT INTO news_radar_item VALUES ('n1','2026-07-07','robot','robot catalyst')")
        con.execute("CREATE TABLE qlib_shadow_evaluation(model_id VARCHAR, sample_count INTEGER, hit_rate DOUBLE)")
        con.execute("INSERT INTO qlib_shadow_evaluation VALUES ('shadow.alstm',20,55.0)")
        con.execute("CREATE TABLE watchlist(trade_date VARCHAR, stock_code VARCHAR)")
        con.execute("INSERT INTO watchlist VALUES ('2026-07-07','000001')")
        con.execute("CREATE TABLE trade_plan(trade_date VARCHAR, stock_code VARCHAR)")
        con.execute("INSERT INTO trade_plan VALUES ('2026-07-07','000001')")
        con.execute("CREATE TABLE risk_snapshot(trade_date VARCHAR, risk_state VARCHAR)")
        con.execute("INSERT INTO risk_snapshot VALUES ('2026-07-07','defensive')")
        con.execute("CREATE TABLE trade_journal(trade_date VARCHAR, stock_code VARCHAR)")
        con.execute("INSERT INTO trade_journal VALUES ('2026-07-07','000001')")
        con.execute(
            """
            CREATE TABLE operator_trade_outcome(
                trade_date VARCHAR, stock_code VARCHAR, execution_status VARCHAR,
                net_return_pct DOUBLE, outcome_tag VARCHAR, mistake_tag VARCHAR
            )
            """
        )
        con.execute("INSERT INTO operator_trade_outcome VALUES ('2026-07-07','000001','executed',1.5,'followed_plan','none')")
        con.execute(
            """
            CREATE TABLE api_endpoint_inventory(
                endpoint VARCHAR, table_name VARCHAR, table_rows INTEGER, verdict VARCHAR, usefulness VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO api_endpoint_inventory VALUES "
            "('/l2/tick-history','l2_tick_history',10,'stable_available','professional_core'),"
            "('/news/theme','news_theme',0,'api_available','professional_useful')"
        )
    finally:
        con.close()

    snapshot = build_operator_report_snapshot(db_path, trade_date="2026-07-07")

    assert snapshot["trade_date"] == "2026-07-07"
    assert snapshot["data_layer"]["source_count"] == 2
    assert snapshot["candidate_layer"]["candidate_count"] == 1
    assert snapshot["research_layer"]["news_count"] == 1
    assert snapshot["qlib_shadow"]["sample_count"] == 20
    assert snapshot["api_utilization"]["discovered_endpoint_count"] == 2
    assert snapshot["api_utilization"]["landed_endpoint_count"] == 1
    assert snapshot["api_utilization"]["high_value_unused_count"] == 1
    assert snapshot["operator_loop"]["watchlist_count"] == 1
    assert snapshot["operator_loop"]["trade_plan_count"] == 1
    assert snapshot["operator_loop"]["risk_snapshot_count"] == 1
    assert snapshot["operator_loop"]["trade_journal_count"] == 1
    assert snapshot["operator_loop"]["outcome_count"] == 1
    assert snapshot["operator_loop"]["executed_outcome_count"] == 1
    assert "auction not confirmed" in snapshot["candidate_layer"]["top_candidates"][0]["invalid_conditions"]


def test_operator_report_persists_snapshot_and_renders_evidence(tmp_path):
    db_path = tmp_path / "operator_report.duckdb"
    snapshot = {
        "trade_date": "2026-07-07",
        "data_layer": {"source_count": 1, "enabled_count": 1},
        "candidate_layer": {
            "candidate_count": 1,
            "top_candidates": [
                {
                    "symbol": "000001",
                    "stock_name": "Test Stock",
                    "stage": "pre_market",
                    "score": 80,
                    "selected_reason": "theme strong",
                    "risk_points": "weak market downgrade",
                    "invalid_conditions": "auction not confirmed",
                }
            ],
        },
        "research_layer": {"news_count": 0, "note_count": 0, "report_count": 0},
        "strategy_backtest": {"sample_count": 10, "avg_win_rate": 60.0},
        "qlib_shadow": {"sample_count": 20, "models": 1, "signal_impact": "disabled"},
        "risk_layer": {"risk_note": "weak market downgrade"},
        "operator_loop": {
            "watchlist_count": 1,
            "trade_plan_count": 1,
            "risk_snapshot_count": 1,
            "trade_journal_count": 1,
            "outcome_count": 1,
            "executed_outcome_count": 1,
            "avg_outcome_net_return_pct": 1.5,
        },
        "api_utilization": {
            "discovered_endpoint_count": 2,
            "available_endpoint_count": 2,
            "landed_endpoint_count": 1,
            "signal_endpoint_count": 1,
            "high_value_unused_count": 1,
            "verdict_counts": {"stable_available": 1, "api_available": 1},
            "workflow_evidence": {"intraday": ["/l2/tick-history"], "post_market": ["/news/theme"]},
            "degradation": [],
        },
    }

    assert persist_operator_report_snapshot(db_path, "daily_operator", snapshot) == 1
    assert persist_operator_report_snapshot(db_path, "daily_operator", snapshot) == 1
    markdown = render_operator_report_markdown(snapshot)

    assert "# Professional Operator Report" in markdown
    assert "API Utilization" in markdown
    assert "Operator Loop" in markdown
    assert "Operator Outcomes" in markdown
    assert "theme strong" in markdown
    assert "weak market downgrade" in markdown
    assert "auction not confirmed" in markdown
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM operator_report_snapshot").fetchone()[0] == 1
    finally:
        con.close()
