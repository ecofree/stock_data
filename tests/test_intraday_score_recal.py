"""Intraday score recalibration unit tests."""

from trade_system.stage_signals import (
    FEATURE_VERSION,
    INTRADAY_FOLLOW_THRESHOLD,
    INTRADAY_FOLLOW_THRESHOLD_LIVE,
    _change_pct_score,
    _compose_intraday_stage_score,
    _compute_intraday_strength,
    _yuan_flow_score,
)


def test_feature_version_bumped():
    assert FEATURE_VERSION.startswith("stage_v3")


def test_yuan_flow_score_log_scale():
    assert _yuan_flow_score(None) == 0.0
    assert _yuan_flow_score(-1) == 0.0
    assert _yuan_flow_score(1e5) == 0.0
    # 1e6 → 20, 1e7 → 40, 1e8 → 60, 1e9 → 80
    assert abs(_yuan_flow_score(1e6) - 20.0) < 0.01
    assert abs(_yuan_flow_score(1e7) - 40.0) < 0.01
    assert abs(_yuan_flow_score(1e8) - 60.0) < 0.01
    assert abs(_yuan_flow_score(1e9) - 80.0) < 0.01
    assert _yuan_flow_score(1e12) == 100.0
    # Old linear scale left 5e6 yuan at 0.5; log scale is usable.
    assert _yuan_flow_score(5e6) > 30.0


def test_change_pct_score():
    assert _change_pct_score(None) == 40.0
    assert abs(_change_pct_score(0.0) - 40.0) < 0.01
    assert abs(_change_pct_score(5.0) - 80.0) < 0.01
    assert _change_pct_score(20.0) == 100.0
    assert _change_pct_score(-10.0) == 0.0


def test_compute_intraday_strength_uses_log_money_and_change():
    # ~5e7 yuan main net + +5% change should land mid/high strength.
    out = _compute_intraday_strength(
        {
            "stock_flow_main_net": 5e7,
            "stock_flow_change_pct": 5.0,
        }
    )
    assert out["money_score"] > 50.0
    assert abs(out["change_score"] - 80.0) < 0.01
    assert 50.0 < out["strength_score"] < 90.0


def test_compose_follow_threshold_with_live_price():
    # source 70, strength 55, live → ~62 + 4 bonus ≈ 66 → follow at 58
    score, decision, thr = _compose_intraday_stage_score(70, 55, has_live_price=True)
    assert thr == INTRADAY_FOLLOW_THRESHOLD_LIVE
    assert score >= thr
    assert decision == "follow"

    # Weak flow stays watch even with live price.
    score2, decision2, thr2 = _compose_intraday_stage_score(40, 20, has_live_price=True)
    assert decision2 == "watch"
    assert score2 < thr2

    # Without live price, bar is higher.
    score3, decision3, thr3 = _compose_intraday_stage_score(70, 50, has_live_price=False)
    assert thr3 == INTRADAY_FOLLOW_THRESHOLD
    # 70*0.4 + 50*0.6 = 58 < 62 → watch
    assert decision3 == "watch"
