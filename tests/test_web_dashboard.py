
import duckdb

from trade_system.web_report import load_dashboard_context, render_dashboard_html


def test_render_dashboard_html_contains_operator_sections():
    context = {
        "generated_at": "2026-07-07 17:00:00",
        "trade_date": "2026-07-06",
        "market": {"regime": "退潮", "suggested_position_pct": 5, "regime_score": 22.5},
        "data_chains": [{"chain": "集合竞价", "status": "available", "required_present": ["auction_bidding_anomaly"], "fallback_present": [], "required_missing": []}],
        "counts": {"kline": 403, "stock_candidate_stage_signal": 284},
        "sources": {"v_index_state": [{"source_table": "l2_realtime_index_list", "is_fallback": False, "count": 4}]},
        "stage_stats": {"premarket_pool": {"sample_count": 71, "return_sample_count": 24, "hit_rate": 16.67, "avg_forward_return_pct": -0.48}},
        "sectors": [{"sector_name": "测试板块", "score": 88.0}],
        "candidates": [{"stock_name": "测试股票", "stock_code": "000001", "score": 77.0}],
        "alerts": [{"severity": "P0", "category": "market", "message": "reduce"}],
        "reports": [{"name": "data_quality_latest.md", "path": "reports/data_quality_latest.md", "size": 1234}],
        "gaps": ["auction_tick is empty"],
    }

    html = render_dashboard_html(context)

    assert "职业交易检查页" in html
    assert "退潮" in html
    assert "集合竞价" in html
    assert "premarket_pool" in html
    assert "data_quality_latest.md" in html
    assert "auction_tick is empty" in html


def test_load_dashboard_context_reads_database_counts_sources_and_reports(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "data_quality_latest.md").write_text("quality", encoding="utf-8")
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE market_regime_snapshot(trade_date VARCHAR, regime VARCHAR, suggested_position_pct INTEGER, regime_score DOUBLE, generated_at TIMESTAMP)")
    con.execute("INSERT INTO market_regime_snapshot VALUES ('2026-07-06', '退潮', 5, 22.5, '2026-07-06 15:00:00')")
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO kline VALUES ('2026-07-06', '000001')")
    con.execute("CREATE VIEW v_index_state AS SELECT 'l2_realtime_index_list' AS source_table, false AS is_fallback")
    con.execute("CREATE TABLE l2_realtime_index_list(date DATE, index_code VARCHAR)")
    con.execute("INSERT INTO l2_realtime_index_list VALUES ('2026-07-06', 'SH000001')")
    con.execute("CREATE TABLE stock_candidate_stage_signal(trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, decision VARCHAR)")
    con.execute("INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-06', 'premarket_pool', '000001', '测试股票', 80, 'watch')")
    con.close()

    context = load_dashboard_context(str(db_path), reports_dir)

    assert context["trade_date"] == "2026-07-06"
    assert context["counts"]["kline"] == 1
    assert context["sources"]["v_index_state"] == [{"source_table": "l2_realtime_index_list", "is_fallback": False, "count": 1}]
    assert context["stage_stats"]["premarket_pool"]["sample_count"] == 1
    assert context["reports"][0]["name"] == "data_quality_latest.md"
    assert context["capital_flow"]["stock"] == []
    assert context["concept_status"]["concepts"] == 0
    assert context["outcome_status"]["outcomes"] == 0
    assert context["qlib_status"]["signal_impact"] == "disabled"
    assert "readiness" in context


def test_dashboard_context_includes_operator_candidate_origin(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE VIEW v_operator_candidates AS "
        "SELECT '2026-07-06' AS trade_date, 'premarket_pool' AS stage, "
        "'000001' AS stock_code, 'native' AS stock_name, 90.0 AS score, "
        "'watch' AS decision, 'stock_data' AS data_origin"
    )
    con.close()

    context = load_dashboard_context(str(db_path), reports_dir)
    html = render_dashboard_html(context)

    assert context["operator_candidates"][0]["data_origin"] == "stock_data"
    assert context["operator_origin_stats"] == [{"data_origin": "stock_data", "count": 1}]
    assert "Unified Operator Candidates" in html


def test_render_dashboard_html_contains_five_operator_workflow_sections():
    context = {
        "generated_at": "2026-07-07 17:00:00",
        "trade_date": "2026-07-07",
        "market": {"regime": "修复", "suggested_position_pct": 30, "regime_score": 62.5},
        "data_chains": [],
        "counts": {"auction_tick": 0, "risk_snapshot": 0},
        "sources": {},
        "stage_stats": {},
        "sectors": [],
        "candidates": [],
        "alerts": [],
        "reports": [],
        "gaps": [],
        "operator_candidates": [],
        "operator_origin_stats": [],
        "strategy_candidates": [
            {
                "trade_date": "2026-07-07",
                "stage": "pre_market",
                "symbol": "000001",
                "stock_name": "测试股份",
                "score": 80,
                "selected_reason": "题材强",
                "risk_points": "弱市场降级",
                "invalid_conditions": "竞价不确认",
            }
        ],
        "auction_evidence": [
            {
                "trade_date": "2026-07-07",
                "stock_code": "000001",
                "source_table": "auction_bidding_anomaly",
                "confirmation": "anomaly_confirmed",
                "auction_strength": 12.34,
                "is_fallback": True,
                "missing_reason": "auction_tick_missing",
            }
        ],
        "research_context": [{"context_type": "news", "sector": "robot", "title": "机器人催化"}],
        "strategy_backtest": [{"strategy_id": "stage.pre", "stage": "pre_market", "sample_count": 10, "win_rate": 60.0}],
        "qlib_shadow": [{"model_id": "shadow.alstm", "sample_count": 0, "hit_rate": None}],
    }

    html = render_dashboard_html(context)

    for label in ["盘前", "竞价", "盘中", "尾盘", "盘后"]:
        assert label in html
    assert "入选原因" in html
    assert "风险点" in html
    assert "失效条件" in html
    assert "auction_bidding_anomaly" in html
    assert "auction_tick_missing" in html
    assert "机器人催化" in html
    assert "qlib shadow" in html
