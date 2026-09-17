"""Tests for red-line screening logic and seal quality scoring."""
from __future__ import annotations

from trade_system.redline_screen import _r1, _r3, _r7




def test_r1_large_cash_large_debt():
    hit, detail = _r1({"monetary_funds": 30, "total_assets": 100,
                       "interest_bearing_debt": 40})
    assert hit and "货币资金" in detail
    ok, _ = _r1({"monetary_funds": 10, "total_assets": 100,
                 "interest_bearing_debt": 10})
    assert not ok


def test_r3_goodwill_thresholds():
    hit, d = _r3({"goodwill": 50, "net_assets": 100})
    assert hit and "红灯" in d
    hit2, d2 = _r3({"goodwill": 30, "net_assets": 100})
    assert hit2 and "黄灯" in d2
    ok, _ = _r3({"goodwill": 10, "net_assets": 100})
    assert not ok


def test_r7_cash_flow_ratio():
    hit, d = _r7({"operating_cash_flow": 50, "net_profit": 100})
    assert hit and "0.5" in d
    ok, _ = _r7({"operating_cash_flow": 120, "net_profit": 100})
    assert not ok
