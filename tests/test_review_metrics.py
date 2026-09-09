from trade_system.review_metrics import (
    COMPOSITE_SCORE_STATUS,
    experimental_theme_score,
    metric_contract,
    validate_experimental_theme_scores,
)


def test_theme_composite_score_is_explicitly_experimental_and_never_formal():
    peers = [
        {"limit_up_count": 1, "max_board": 1, "persistence_days": 1, "main_net": -1},
        {"limit_up_count": 5, "max_board": 3, "persistence_days": 3, "main_net": 10},
    ]
    result = experimental_theme_score(peers[1], peers)

    assert result["status"] == COMPOSITE_SCORE_STATUS
    assert result["score"] is not None
    assert result["formal_ready"] is False
    assert metric_contract()["composite_score"]["formal_ready"] is False


def test_theme_score_does_not_impute_missing_components():
    result = experimental_theme_score({"limit_up_count": 5}, [{"limit_up_count": 5}])

    assert result["score"] is None
    assert set(result["missing_components"]) == {"height", "persistence", "flow"}


def test_validation_threshold_does_not_promote_score():
    samples = [
        {"trade_date": f"2026-08-{index:02d}", "score": index, "forward_return_pct": index / 10}
        for index in range(1, 61)
    ]
    result = validate_experimental_theme_scores(samples, min_samples=60, min_dates=20)

    assert result["status"] == "validation_ready"
    assert result["formal_ready"] is False
    assert result["decision"] == "remain_experimental"

