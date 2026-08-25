"""Trader decision signals: five real-time alerts for the war-room command bar.

Each function returns (status, detail) where status is one of:
  "good" / "warn" / "danger" / "info" / "nodata"

These signals answer the trader's first question before anything else:
"今天是什么行情？我该做什么？"
"""
from __future__ import annotations



def premium_traffic_light(premium_pct: float | None) -> dict:
    """昨日涨停溢价率红绿灯。>0% 绿灯可打板 / -2%~0% 黄灯谨慎 / <-2% 红灯空仓."""
    if premium_pct is None:
        return {"status": "nodata", "label": "溢价率", "value": "—", "detail": "暂无数据"}
    if premium_pct >= 0:
        return {"status": "good", "label": "涨停溢价", "value": f"{premium_pct:+.1f}%",
                "detail": "打板期望值为正，可参与"}
    if premium_pct >= -2:
        return {"status": "warn", "label": "涨停溢价", "value": f"{premium_pct:+.1f}%",
                "detail": "溢价转负，降低接力预期"}
    return {"status": "danger", "label": "涨停溢价", "value": f"{premium_pct:+.1f}%",
            "detail": "亏钱效应显著，建议空仓观望"}


def ladder_break_detector(today_max_board: int | None,
                          prev_max_board: int | None) -> dict:
    """连板高度断层：今日最高板 << 昨日最高板 = 梯队断裂."""
    if today_max_board is None or prev_max_board is None:
        return {"status": "nodata", "label": "连板高度", "value": "—", "detail": "数据不足"}
    if today_max_board >= prev_max_board:
        return {"status": "good", "label": "连板高度",
                "value": f"{today_max_board}板",
                "detail": f"与前日持平或上升(前日{prev_max_board}板)"}
    ratio = today_max_board / prev_max_board if prev_max_board else 1
    if ratio >= 0.6:
        return {"status": "warn", "label": "连板高度",
                "value": f"{today_max_board}板",
                "detail": f"较前日{prev_max_board}板回落"}
    return {"status": "danger", "label": "连板断层",
            "value": f"{today_max_board}板←{prev_max_board}板",
            "detail": "梯队断裂，情绪周期可能进入退潮"}


def seal_ratio_alert(seal_money: float | None, turnover_amount: float | None) -> dict:
    """封成比 = 封单额 / 当日成交额。>1 强封锁 / <0.3 易炸."""
    if not seal_money or not turnover_amount or turnover_amount == 0:
        return {"status": "nodata", "label": "封成比", "value": "—", "detail": "数据不足"}
    ratio = round(seal_money / turnover_amount, 2)
    if ratio >= 1.0:
        return {"status": "good", "label": "封成比", "value": str(ratio),
                "detail": "强封锁，抛压被完全消化"}
    if ratio >= 0.3:
        return {"status": "warn", "label": "封成比", "value": str(ratio),
                "detail": "封单偏弱，注意开板风险"}
    return {"status": "danger", "label": "封成比", "value": str(ratio),
            "detail": "封单严重不足，高开炸板概率大"}


def lhb_resonance_detect(agency_buy: float | None,
                         youzi_buy: float | None) -> dict:
    """龙虎榜共振板：机构专用≥5000万 且 知名游资净买 = 最强信号."""
    if agency_buy is None and youzi_buy is None:
        return {"status": "nodata", "label": "龙虎榜", "value": "—", "detail": "未上榜"}
    agency_ok = agency_buy is not None and agency_buy > 50_000_000
    youzi_ok = youzi_buy is not None and youzi_buy > 0
    if agency_ok and youzi_ok:
        return {"status": "good", "label": "龙虎榜共振",
                "value": "机构+游资", "detail": "基本面与短线情绪强共振，次日溢价率最高"}
    if agency_ok:
        return {"status": "info", "label": "龙虎榜", "value": "机构买入",
                "detail": "机构定价权持续性好但缺乏短线弹性"}
    return {"status": "info", "label": "龙虎榜", "value": "游资活跃",
            "detail": "纯情绪博弈，持续性待验证"}


NEGATIVE_LIST_RULES = [
    ("is_st", "ST/*ST"),
    ("market_cap_low", "总市值<15亿"),
    ("investigation", "被证监会立案"),
]


def negative_list_filter(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate candidates into pass/fail based on hard exclusion rules."""
    passed, rejected = [], []
    for r in rows:
        reasons = []
        if r.get("is_st"):
            reasons.append("ST/*ST")
        close = r.get("close") or 0
        # Market cap proxy: close * shares would need float_shares
        # For now use price threshold as rough proxy
        if close and close < 2.0:
            reasons.append("股价<2元(面值退市风险)")
        if reasons:
            rejected.append({**r, "reject_reasons": reasons})
        else:
            passed.append(r)
    return passed, rejected
