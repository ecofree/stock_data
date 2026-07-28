from trade_system.review import (
    render_candidate_report,
    render_market_status_report,
    render_risk_alert_report,
    render_sector_mainline_report,
)


def test_professional_reports_have_operator_sections():
    market = render_market_status_report(
        "2026-07-06",
        {"regime": "退潮", "suggested_position_pct": 5, "evidence_json": '{"limit_down_count": 45}'},
    )
    sector = render_sector_mainline_report(
        "2026-07-06",
        [{"sector_name": "test sector", "score": 88, "evidence_json": '{"score_components": {"a": 1}}'}],
    )
    candidate = render_candidate_report(
        "2026-07-06",
        [{"stock_name": "test stock", "score": 77, "evidence_json": '{"entry_reason":"x","risk_points":["y"],"invalidation":"z"}'}],
    )
    risk = render_risk_alert_report(
        "2026-07-06",
        [{"severity": "P0", "category": "market", "message": "reduce", "evidence_json": "{}"}],
    )

    assert "市场状态报告" in market
    assert "证据" in market
    assert "板块主线报告" in sector
    assert "score_components" in sector
    assert "候选股报告" in candidate
    assert "失效条件" in candidate
    assert "风险告警报告" in risk
    assert "P0" in risk
