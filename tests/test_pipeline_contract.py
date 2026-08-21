from trade_system.pipeline_contract import (
    RESEARCH_CHAIN_STEPS,
    REVIEW_CHAIN_STEPS,
)


def test_research_and_review_chains_are_disjoint_and_explicit():
    assert RESEARCH_CHAIN_STEPS
    assert REVIEW_CHAIN_STEPS
    assert RESEARCH_CHAIN_STEPS.isdisjoint(REVIEW_CHAIN_STEPS)
    assert "run_qlib_daily" in RESEARCH_CHAIN_STEPS
    assert "generate_daily_review_web" in REVIEW_CHAIN_STEPS
