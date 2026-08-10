from trade_system.terminal_report import build_terminal_context, render_terminal_html, write_terminal


def _sample_context() -> dict:
    return {
        "meta": {"trade_date": "2026-07-29", "generated_at": "2026-07-29 10:00:00",
                 "regime": "启动", "regime_score": 55, "position_pct": 45},
        "breadth": {"limit_up": 45, "limit_down": 8, "rise": 3000, "fall": 1800,
                    "consecutive_height": 6, "earning_effect": 55, "acute_drop": 20, "blown_rate": 0.15},
        "emotion_history": [
            {"d": f"2026-07-{i:02d}", "lu": 30 + i, "ld": 5, "rise": 3000, "fall": 1800,
             "ch": 4, "ee": 50, "ad": 20, "regime": "启动", "score": 55, "fb": False}
            for i in range(1, 21)],
        "candidates": {
            "pool_size": 118, "evaluated": 131,
            "actionable": [{"code": "600519", "name": "贵州茅台", "stage": "intraday_strength",
                            "score": 88, "decision": "follow", "reason": None, "executable": True}],
            "blocked": [{"code": "000001", "name": "平安银行", "stage": "intraday_strength",
                         "score": 60, "decision": "blocked_data_quality",
                         "reason": "delayed_provider_not_executable", "executable": False}],
            "block_reasons": {"delayed_provider_not_executable": 30}, "trend": []},
        "sector_flow_today": {
            "inflow": [{"name": "半导体", "main": 1.2e8, "lu": 8, "seal": 0.8, "fb": False}],
            "outflow": [{"name": "白酒", "main": -5e7, "lu": 1, "seal": 0.3, "fb": False}],
            "count": 870},
        "stock_flow_today": {
            "inflow": [{"code": "600519", "name": "贵州茅台", "main": 2e8, "super": 1e8,
                        "large": 5e7, "chg": 5.2, "turn": 3e9, "provider": "eastmoney_intraday_clist"}],
            "outflow": [], "count": 5517},
        "sector_flow_history": {"dates": ["2026-07-28", "2026-07-29"], "sectors": ["半导体"],
                                "codes": ["x"], "matrix": [[1e7, 1.2e8]]},
        "ladder": [{"level": 3, "count": 2, "stocks": [{"code": "a", "name": "A", "time": "09:30"}]},
                   {"level": 1, "count": 40, "stocks": []}],
        "concepts": [{"code": "c1", "name": "人工智能", "score": 92, "strength": 88, "lu": 12,
                      "main": 3e8, "members": 45, "reason": "大模型"}],
        "index_kline": {"SH000001": {"name": "上证指数", "data": [
            ["2026-07-28", 3000, 3050, 2990, 3060, 1.2],
            ["2026-07-29", 3050, 3080, 3040, 3090, 0.98]]}},
        "auction": {"anomalies": [{"code": "002384", "type": "bidding_amount", "value": 1.32e8}],
                    "tape": [], "tape_stocks": []},
        "lhb": [{"date": "2026-07-28", "code": "600519", "name": "贵州茅台", "reason": "日涨幅偏离",
                 "net": 1e8, "brokers": 5, "youzi": 5e7, "agency": 3e7, "prob": 0.9}],
        "data_health": {"chains": [{"chain": "market_state", "status": "available"}],
                        "freshness": [{"label": "市场状态", "latest": "2026-07-29"}],
                        "reconciliation": {"status": "pass", "reference_rows": 5000},
                        "independent_source_codes": 5000},
        "alerts": [{"severity": "P1", "category": "risk",
                    "message": "Acute drop risk score is 66.2", "evidence": "{}"}],
        "stage_validation": {"sample_count": 123,
                             "stage_statistics": {"intraday_strength": {
                                 "sample_count": 123, "return_sample_count": 122,
                                 "hit_rate": 31.97, "avg_forward_return_pct": -1.29,
                                 "verdict": "negative_sample"}},
                             "regime_stage_counts": {"启动": {"intraday_strength": 40}}},
        "plan_console": {"rows": [{"code": "600519", "name": "贵州茅台", "setup": "breakout",
                                   "maxpos": 10.0, "status": "active", "entry": "突破买入",
                                   "stop": "跌破5日线", "thesis": "主线龙头",
                                   "invalidation": "放量滞涨"}],
                         "risk": {"state": "defensive", "total": 0.0, "single": 5.0,
                                  "sector": 20.0}},
        "blown_history": [{"d": f"2026-07-{i:02d}", "broken": 40 + i, "blown": 20,
                           "rate": 15.0 + i} for i in range(1, 21)],
        "promotion": {"series": [{"d": f"2026-07-{i:02d}", "l1_rate": 20.0 + i,
                                  "hi_rate": 30.0 + i, "l1": 40, "hi": 10}
                                 for i in range(1, 11)],
                      "premium": 1.25, "latest": {"d": "2026-07-10", "l1_rate": 30.0,
                                                  "hi_rate": 40.0}},
        "auction_confirmation": {"rows": [{"stock_code": "600105", "name": "永鼎股份",
                                           "auction_strength": 3.2, "auction_amount": 1.1e8,
                                           "confirmation": "tick_confirmed", "tick_rows": 69,
                                           "source_table": "auction_tick"}],
                                 "counts": {"tick_confirmed": 85, "missing": 34}, "gaps": []},
        "flow_coverage": {"ready": True,
                          "stock_flow": {"ready": True, "observed": 5523, "expected": 5539,
                                         "coverage": 99.7, "relations": [
                                             {"relation": "multi_source_stock_flow", "rows": 10732,
                                              "codes": 5523, "status": "ready"}]},
                          "sector_flow": {"ready": True, "observed": 1901, "expected": 870,
                                          "coverage": 100.0, "relations": []},
                          "reconciliation": {"status": "pass", "reference_rows": 5188,
                                             "independent_source_present": True,
                                             "independent_reconciliation_ready": True}},
        "pipeline_matrix": {"sessions": [{"d": "2026-07-29", "passed": False,
                                          "checks": {"calendar": True, "auction_run": False,
                                                     "close_run": True}}],
                            "consecutive": 0, "ready_for_p1": False},
        "northbound": {"intraday": {"time": ["09:30", "09:31"], "hgt": [1.2, 1.5],
                                    "sgt": [2.0, 2.4], "total": [3.2, 3.9],
                                    "latest_total": 3.9},
                       "intraday_date": "2026-07-29"},
    }


def test_terminal_render_structure():
    html = render_terminal_html(_sample_context())
    assert html.startswith("<!doctype")
    # DATA injected exactly once, no leftover placeholder.
    assert html.count("const DATA=") == 1
    assert "__DATA__" not in html
    # ECharts loaded + all chart containers present.
    assert "echarts.min.js" in html
    for cid in ["chart-emotion", "chart-emotion-money", "chart-funnel", "chart-block",
                "chart-sector", "chart-heatmap", "chart-auction", "chart-blown",
                "chart-promotion", "chart-north", "chart-idx-SH000001"]:
        assert f'id="{cid}"' in html, cid
    # Key content rendered (A-share red-up convention applied via classes elsewhere).
    for token in ["交易作战室", "贵州茅台", "人工智能", "连板梯队", "龙虎榜", "数据健康",
                  "阶段信号验证", "操盘计划执行台", "炸板封板体检", "赚钱效应",
                  "连板晋级体检", "竞价确认榜", "资金流覆盖率", "流水线体检矩阵",
                  "北向资金", "Acute drop risk", "永鼎股份"]:
        assert token in html, token


def test_terminal_context_and_write_end_to_end(tmp_path):
    import duckdb
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE v_market_state_inputs (trade_date DATE, limit_up_count INTEGER, "
        "limit_down_count INTEGER, rise_count INTEGER, fall_count INTEGER, "
        "consecutive_count INTEGER, earning_effect_score DOUBLE, acute_drop_risk_score DOUBLE, "
        "is_fallback BOOLEAN, cgl DOUBLE, yll DOUBLE, success_rate DOUBLE)")
    con.execute(
        "INSERT INTO v_market_state_inputs VALUES "
        "(DATE '2026-07-28', 40, 10, 2900, 1900, 5, 50, 22, false, 45, 55, 52),"
        "(DATE '2026-07-29', 45, 8, 3000, 1800, 6, 55, 20, false, 48, 57, 55)")
    con.close()

    ctx = build_terminal_context(str(db), "2026-07-29")
    assert ctx["meta"]["regime"] is not None          # computed from breadth
    assert len(ctx["emotion_history"]) == 2
    html = render_terminal_html(ctx)
    assert html.startswith("<!doctype") and html.count("const DATA=") == 1

    out = tmp_path / "out.html"
    written = write_terminal(str(db), str(out), "2026-07-29")
    assert written.exists() and written.stat().st_size > 1000
