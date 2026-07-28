"""Strategy definition protocol inspired by tickflow-stock-panel."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    name: str
    stage: str
    min_score: float
    entry_rules: list[str] = field(default_factory=list)
    risk_rules: list[str] = field(default_factory=list)
    invalid_conditions: list[str] = field(default_factory=list)
    score_field: str = "score"


def load_default_stage_strategies() -> list[StrategyDefinition]:
    return [
        StrategyDefinition(
            strategy_id="stage.pre_market_strength",
            name="盘前题材强势池",
            stage="pre_market",
            min_score=70,
            entry_rules=["候选分 >= 70", "题材/板块证据有效"],
            risk_rules=["弱市场降级观察", "题材连续高潮后降低仓位"],
            invalid_conditions=["竞价不确认", "板块主线转弱", "风险公告触发"],
        ),
        StrategyDefinition(
            strategy_id="stage.auction_confirm",
            name="竞价确认",
            stage="auction_confirm",
            min_score=72,
            entry_rules=["竞价强度确认", "竞价金额和开盘承接不弱"],
            risk_rules=["高开低走风险", "竞价撤单异常"],
            invalid_conditions=["开盘跌破竞价承接线", "同题材核心明显走弱"],
        ),
        StrategyDefinition(
            strategy_id="stage.intraday_strength",
            name="盘中强弱",
            stage="intraday_strength",
            min_score=68,
            entry_rules=["分时承接有效", "板块强度保持"],
            risk_rules=["炸板率升高", "指数急跌"],
            invalid_conditions=["跌破分时均线且板块转弱", "大单流出持续扩大"],
        ),
        StrategyDefinition(
            strategy_id="stage.closing_decision",
            name="尾盘去留",
            stage="closing_decision",
            min_score=65,
            entry_rules=["尾盘承接不弱", "次日预期仍在"],
            risk_rules=["尾盘抢筹失败", "隔夜风险事件"],
            invalid_conditions=["收盘弱于板块", "次日预期消失"],
        ),
    ]
