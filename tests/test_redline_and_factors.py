"""Tests for red-line screening logic and seal quality scoring."""
from __future__ import annotations

from trade_system.redline_screen import _r1, _r3, _r7
from trade_system.stock_screener import score_candidates


def _row(code, **kw):
    base = {"stock_code": code, "stock_name": code, "board": 1,
            "qlib_score": 0.5, "flow_rank_pct": 0.5, "seal_money": 100,
            "max_seal_money": 200, "open_times": 0,
            "concept_heat": 0.5, "limit_up_reason": "test",
            "pe_ttm": None, "revenue_yoy": None}
    base.update(kw)
    return base


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


def test_seal_quality_open_times_penalty_in_score():
    clean = score_candidates([_row("A", open_times=0)], "divergence")
    reopened = score_candidates([_row("B", open_times=3)], "divergence")
    # The -3 penalty for reopen should make B score lower than A by exactly 3
    diff = clean[0]["total_score"] - reopened[0]["total_score"]
    assert abs(diff - 3.0) < 0.01


def test_valuation_factor_prefers_lower_pe():
    rows = [_row("CHEAP", pe_ttm=8.0), _row("EXPENSIVE", pe_ttm=80.0)]
    result = score_candidates(rows, "divergence")
    fj = {p["stock_code"]: p["factor_json"] for p in result}
    # Valuation component should favor lower PE
    assert fj["CHEAP"]["valuation"] > fj["EXPENSIVE"]["valuation"]
    # Earnings should be equal (both None)
    assert fj["CHEAP"].get("earnings", 0) == fj["EXPENSIVE"].get("earnings", 0)


def test_earnings_yoy_positive_gets_bonus():
    rows = [_row("GROWER", revenue_yoy=50.0), _row("DECLINER", revenue_yoy=-20.0)]
    result = score_candidates(rows, "divergence")
    fj = {p["stock_code"]: p["factor_json"] for p in result}
    assert fj["GROWER"]["earnings"] > fj["DECLINER"]["earnings"]
