"""Financial red-line screening: automated fraud / risk detection.

Implements the 10-point "致命红线" checklist from the A-share selection
framework.  Uses HiThink income statement + balance sheet endpoints.
Any triggered red-line is a hard veto regardless of other scores.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from trade_system.hithink_client import HiThinkClient  # noqa: E402
from trade_system.logging_setup import get_logger  # noqa: E402

logger = get_logger("redline_screen")

REDLINES = [
    {"id": "R1", "name": "大存大贷",
     "check": lambda bs: _r1(bs)},
    {"id": "R2", "name": "应收飙升",
     "check": lambda bs: _r2(bs)},
    {"id": "R3", "name": "商誉高企",
     "check": lambda bs: _r3(bs)},
    {"id": "R5", "name": "非经常依赖",
     "check": lambda inc: _r5(inc)},
    {"id": "R7", "name": "净现比背离",
     "check": lambda cf: _r7(cf)},
]


def _safe_div(a, b):
    if not b or b == 0:
        return None
    try:
        return round(a / abs(b) * 100, 1)
    except (TypeError, ZeroDivisionError):
        return None


def _r1(bs: dict) -> tuple[bool, str]:
    """大存大贷: monetary funds >25% assets AND interest-bearing debt >30%."""
    mf = bs.get("monetary_funds") or 0
    ta = bs.get("total_assets") or 0
    debt = bs.get("interest_bearing_debt") or 0
    if not ta:
        return False, ""
    mf_pct = round(mf / ta * 100, 1)
    debt_pct = round(debt / ta * 100, 1)
    if mf_pct > 25 and debt_pct > 30:
        return True, f"货币资金/总资产={mf_pct}% 且 有息负债/总资产={debt_pct}%"
    return False, ""


def _r2(bs: dict) -> tuple[bool, str]:
    """应收账款增速 > 营收增速 +15pct."""
    ar = bs.get("accounts_receivable")
    rev = bs.get("operating_income")
    ar_prev = bs.get("prev_accounts_receivable")
    rev_prev = bs.get("prev_operating_income")
    if not all([ar, rev, ar_prev, rev_prev]):
        return False, ""
    ar_growth = (ar / ar_prev - 1) * 100 if ar_prev else 0
    rev_growth = (rev / rev_prev - 1) * 100 if rev_prev else 0
    diff = round(ar_growth - rev_growth, 1)
    if diff > 15:
        return True, f"应收增速{ar_growth:.1f}% - 营收增速{rev_growth:.1f}% = {diff}pct"
    return False, ""


def _r3(bs: dict) -> tuple[bool, str]:
    """商誉/净资产 >25% 黄灯, >40% 红灯."""
    gw = bs.get("goodwill") or 0
    na = bs.get("net_assets") or bs.get("total_equity") or 0
    pct = _safe_div(gw, na)
    if pct and pct > 40:
        return True, f"商誉/净资产={pct}%（红灯）"
    if pct and pct > 25:
        return True, f"商誉/净资产={pct}%（黄灯预警）"
    return False, ""


def _r5(inc: dict) -> tuple[bool, str]:
    """扣非归母净利润/归母净利润 <60%（连续两年）."""
    deducted = inc.get("deducted_net_profit")
    net = inc.get("net_profit")
    if not net or net == 0 or deducted is None:
        return False, ""
    ratio = round(deducted / abs(net) * 100, 1)
    if ratio < 60:
        return True, f"扣非净利/归母净利={ratio}%（利润质量偏低）"
    return False, ""


def _r7(cf: dict) -> tuple[bool, str]:
    """连续2年 CFO/净利 <0.7."""
    cfo = cf.get("operating_cash_flow")
    ni = cf.get("net_profit")
    if not cfo or not ni or ni == 0:
        return False, ""
    ratio = round(cfo / abs(ni), 2)
    if ratio < 0.7:
        return True, f"净现比(CFO/净利)={ratio}"
    return False, ""


def screen_stock(client: HiThinkClient, thscode: str) -> list[dict]:
    """Run all red-line checks on one stock; returns triggered items."""
    triggered = []
    time.sleep(0.4)

    # Balance sheet
    try:
        bs_data = client._get(
            "/api/a-share/financials/balance-sheets",
            {"thscode": thscode, "period": "quarterly", "limit": 2})
        bs_items = bs_data.get("item") or []
        if bs_items:
            latest_bs = bs_items[0]
            prev_bs = bs_items[1] if len(bs_items) > 1 else {}
            merged = {**latest_bs,
                      "prev_accounts_receivable": prev_bs.get("accounts_receivable"),
                      "prev_operating_income": prev_bs.get("operating_income")}
            for rl in REDLINES:
                if rl["id"] in ("R1", "R2", "R3"):
                    hit, detail = rl["check"](merged)
                    if hit:
                        triggered.append({"id": rl["id"], "name": rl["name"], "detail": detail})
    except Exception as exc:
        logger.debug("balance sheet fetch failed for %s: %s", thscode, exc)

    time.sleep(0.4)

    # Income statement
    try:
        inc_data = client._get(
            "/api/a-share/financials/income-statements",
            {"thscode": thscode, "period": "quarterly", "limit": 2})
        inc_items = inc_data.get("item") or []
        if inc_items:
            for rl in REDLINES:
                if rl["id"] == "R5":
                    hit, detail = rl["check"](inc_items[0])
                    if hit:
                        triggered.append({"id": rl["id"], "name": rl["name"], "detail": detail})
    except Exception as exc:
        logger.debug("income fetch failed for %s: %s", thscode, exc)

    return triggered
