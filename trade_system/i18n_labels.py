"""Chinese label mappings for operator-facing reports and dashboards.

The data layer stores stable English enum values and template sentences;
every rendering surface (markdown review, HTML dashboard/review page,
terminal report) translates through this module so operators never see raw
enum tokens.  Unknown values fall back to the original token instead of
guessing.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- enums
SETUP_TYPE_CN = {
    "manual_shortline_plan": "人工短线计划",
}

PLAN_STATUS_CN = {
    "planned": "已列入计划",
    "review_required": "待人工复核",
    "blocked_data_quality": "数据质量受阻",
    "executed": "已执行",
    "skipped": "已跳过",
    "cancelled": "已取消",
}

EXECUTION_STATUS_CN = {
    **PLAN_STATUS_CN,
    "pending": "待执行",
    "filled": "已成交",
    "partial_filled": "部分成交",
}

STAGE_CN = {
    "premarket_pool": "盘前候选池",
    "auction_confirmation": "竞价确认",
    "intraday_strength": "盘中强度",
    "close_decision": "尾盘决策",
}

SELECTION_STATUS_CN = {
    "research_only": "仅供研究",
    "research_only_limit_pool": "仅供研究（来自涨停池）",
    "eligible": "可入选",
}

PROVIDER_CN = {
    "kpl": "开盘啦",
    "sina": "新浪财经",
    "eastmoney": "东方财富",
    "eastmoney_intraday_clist": "东财·盘中列表",
    "eastmoney_intraday_clist_delay": "东财·盘中列表（延迟）",
    "eastmoney_market": "东财·行情",
    "eastmoney_sector_full": "东财·全板块",
    "tushare": "TuShare 中继",
    "tushare_sector_full": "TuShare·全板块",
    "derived_ths_stock_aggregate": "同花顺聚合（推导）",
    "existing_core": "核心采集链路",
    "cache": "本地缓存",
    "baostock": "BaoStock",
    "pytdx": "通达信",
    "tencent": "腾讯财经",
    "baidu": "百度股市通",
    "ths": "同花顺",
    "cls": "财联社",
    "cninfo": "巨潮资讯",
}

SEVERITY_CN = {
    "P0": "P0·紧急",
    "P1": "P1·重要",
    "P2": "P2·提示",
    "info": "提示",
    "warn": "警告",
    "error": "严重",
}

CATEGORY_CN = {
    "data_quality": "数据质量",
    "risk": "风险控制",
    "market_regime": "市场状态",
    "pipeline": "管道运行",
    "coverage": "覆盖度",
}

WATCHLIST_STATUS_CN = {
    "active": "观察中",
    "promoted": "已升级为计划",
    "dropped": "已移出",
    "review_required": "待人工复核",
}

# ------------------------------------------------- free-text translations
# Ordered substring replacements applied to generated English sentences.
# Keys are matched verbatim; first match wins per span (str.replace chain).
_PHRASE_CN = [
    # plan entry condition
    ("Only consider after auction/intraday evidence confirms",
     "需竞价/盘中证据确认后方可考虑"),
    ("risk_gate=", "风控门控："),
    ("candidate score below minimum", "候选得分低于阈值"),
    ("score below phase threshold", "得分低于当前相位阈值"),
    # plan stop / invalidation boilerplate
    ("Invalidate if score/fallback/risk evidence deteriorates.",
     "若得分、兜底数据或风控证据恶化即失效。"),
    ("Invalidate if price falls back below", "若价格回落跌破"),
    ("Invalidate on break of", "若跌破"),
    # plan note
    ("Review at close; no automatic execution.", "仅收盘人工复核，不自动执行。"),
    # concept -> limit-up availability messages
    ("THS concept membership or same-date limit pool is unavailable",
     "同花顺概念成分或当日涨停池数据不可用"),
    ("no THS membership snapshot not later than the review date",
     "复盘日之前没有可用的同花顺概念成分快照"),
    ("THS membership snapshot is stale by",
     "同花顺概念成分快照已滞后"),
    # risk-gate decision reasons (trade_system.operator_risk)
    ("planned position must be positive", "计划仓位必须大于 0"),
    ("total position limit exceeded", "超出总仓位上限"),
    ("single stock position limit exceeded", "超出单票仓位上限"),
    ("candidate score below minimum", "候选得分低于最低分要求"),
    ("weak market regime requires reduced probing position",
     "市场状态偏弱，仅允许试探性轻仓"),
    ("manual risk flags require review", "存在人工风控标记，需复核"),
    # alerts
    ("No sector score rows generated for this date.",
     "该交易日未生成板块评分数据。"),
    ("Acute drop risk score is", "急跌风险分值为"),
    ("tighten intraday risk limits.", "请收紧盘中仓位上限。"),
    ("); ", "）；"),
    ("; ", "；"),
    ("Market regime is", "市场状态判定为"),
    ("reduce exposure.", "建议降低整体敞口。"),
    ("coverage below threshold", "覆盖度低于阈值"),
    ("stale snapshot", "快照过期"),
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+")


def zh_text(value) -> str:
    """Translate known generated phrases; unknown text passes through."""
    if value is None:
        return ""
    text = str(value)
    for en, cnz in _PHRASE_CN:
        if en in text:
            text = text.replace(en, cnz)
    return text


def cn(mapping: dict, value):
    """Map an enum token to Chinese; unknown tokens pass through unchanged."""
    if value is None:
        return None
    return mapping.get(str(value), value)


def zh_reason(reason: str | None) -> str | None:
    """Translate a risk-gate decision reason fragment."""
    if reason is None:
        return None
    return zh_text(str(reason))
