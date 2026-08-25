"""ERP (Equity Risk Premium) dynamic position sizing model.

    ERP = 1 / PE(沪深300) - 10Y Treasury Yield

Five tiers based on rolling 3-year percentile.  10Y yield must be manually
updated monthly (one number) or set via KPL_TREASURY_10Y env.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trade_system.logging_setup import get_logger

logger = get_logger("erp_model")


@dataclass
class ERPTier:
    tier: int           # 1..5
    label: str          # 极度低估/显著低估/合理中枢/市场过热/严重泡沫
    position_pct: int   # 建议权益总仓位 %
    core_pct: int       # 核心底仓占比
    satellite_pct: int  # 卫星弹性占比
    action: str


_TIERS = [
    ERPTier(1, "极度低估·黄金坑", 95, 60, 40, "战略性全面做多，锁死底仓，拒绝恐慌割肉"),
    ERPTier(2, "显著低估",         82, 65, 35, "稳步增加弹性成长标的与行业白马龙头"),
    ERPTier(3, "估值合理中枢",     62, 70, 30, "精选个股，严格执行性价比置换"),
    ERPTier(4, "市场过热",         40, 80, 20, "逐步兑现高估值卫星，增配防御红利"),
    ERPTier(5, "极度泡沫",         10, 100, 0, "大规模止盈清仓，现金为王"),
]


def _percentile_rank(value: float, series: list[float]) -> float:
    """Percentile rank of value within historical series (0..100)."""
    if not series:
        return 50.0
    below = sum(1 for s in series if s < value)
    return round(below / len(series) * 100, 1)


def calculate_erp(
    hs300_pe: float,
    treasury_10y: float,
    erp_history: list[float] | None = None,
) -> dict:
    """Calculate current ERP and return the matching tier recommendation.

    Args:
        hs300_pe: Current PE of 沪深300 index.
        treasury_10y: Current 10-year Chinese government bond yield (%).
        erp_history: Historical ERP values for percentile calculation.

    Returns:
        Dict with erp, percentile, tier recommendation and strategy line.
    """
    if not hs300_pe or hs300_pe <= 0:
        return {"error": "invalid PE"}

    earnings_yield = round(100.0 / hs300_pe, 2)
    erp = round(earnings_yield - treasury_10y, 2)

    hist = erp_history or []
    if len(hist) >= 12:
        mean = sum(hist) / len(hist)
        std = (sum((x - mean) ** 2 for x in hist) / len(hist)) ** 0.5
        pct = _percentile_rank(erp, hist)
    else:
        # Fallback to fixed thresholds when insufficient history
        mean, std = 2.5, 1.5
        pct = None

    # Match to tier by sigma bands
    if std > 0:
        z = (erp - mean) / std
    else:
        z = 0.0

    if z >= 2.0:
        tier = _TIERS[0]
    elif z >= 1.0:
        tier = _TIERS[1]
    elif z >= -1.0:
        tier = _TIERS[2]
    elif z >= -2.0:
        tier = _TIERS[3]
    else:
        tier = _TIERS[4]

    now = datetime.now().strftime("%Y-%m-%d")
    logger.info("ERP=%.2f%% earnings_yield=%.2f%% tier=%d pct=%s",
                erp, earnings_yield, tier.tier, pct)

    return {
        "date": now,
        "hs300_pe": hs300_pe,
        "earnings_yield": earnings_yield,
        "treasury_10y": treasury_10y,
        "erp": erp,
        "erp_percentile": pct,
        "z_score": round(z, 2),
        "tier": tier.tier,
        "label": tier.label,
        "position_pct": tier.position_pct,
        "core_satellite": f"{tier.core_pct}:{tier.satellite_pct}",
        "action": tier.action,
    }
