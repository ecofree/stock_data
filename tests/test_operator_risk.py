from trade_system.operator_risk import OperatorRiskConfig, TradePlanInput, evaluate_trade_plan


def test_evaluate_trade_plan_blocks_when_market_position_limit_is_exceeded():
    config = OperatorRiskConfig(max_total_position_pct=20, max_single_stock_pct=10, min_score=70)
    plan = TradePlanInput(
        stock_code="000001",
        score=85,
        planned_position_pct=15,
        current_total_position_pct=10,
        market_regime="退潮",
    )

    result = evaluate_trade_plan(plan, config)

    assert result.allowed is False
    assert result.severity == "P0"
    assert "total position" in result.reason


def test_evaluate_trade_plan_allows_small_high_score_plan():
    config = OperatorRiskConfig(max_total_position_pct=20, max_single_stock_pct=10, min_score=70)
    plan = TradePlanInput(
        stock_code="000001",
        score=85,
        planned_position_pct=5,
        current_total_position_pct=5,
        market_regime="启动",
    )

    result = evaluate_trade_plan(plan, config)

    assert result.allowed is True
    assert result.severity == "OK"


def test_evaluate_trade_plan_blocks_weak_regime_oversized_probe():
    config = OperatorRiskConfig(max_total_position_pct=20, max_single_stock_pct=10, min_score=70)
    plan = TradePlanInput(
        stock_code="000001",
        score=88,
        planned_position_pct=6,
        current_total_position_pct=2,
        market_regime="冰点",
    )

    result = evaluate_trade_plan(plan, config)

    assert result.allowed is False
    assert result.severity == "P1"
    assert "weak market" in result.reason


def test_evaluate_trade_plan_blocks_zero_position_plan():
    plan = TradePlanInput(
        stock_code="000001",
        score=99,
        planned_position_pct=0,
        current_total_position_pct=0,
        market_regime="数据缺失",
    )

    result = evaluate_trade_plan(plan)

    assert result.allowed is False
    assert result.severity == "P0"
    assert "positive" in result.reason
