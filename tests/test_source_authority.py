from __future__ import annotations

import pytest

from trade_system.source_authority import validate_production_plan


def test_close_plan_requires_official_and_realtime_limit_pool_edges():
    validate_production_plan(
        "close",
        [
            "collect_market_context", "sync_tushare_close",
            "collect_ths_concepts_api", "collect_hithink_limit_pool_daily",
            "collect_realtime_limit_pool", "collect_intraday_stock_flow_market",
            "collect_intraday_sector_flow_full",
        ],
    )


def test_close_plan_rejects_missing_canonical_producer():
    with pytest.raises(ValueError, match="limit_pool"):
        validate_production_plan(
            "close",
            [
                "collect_market_context", "sync_tushare_close",
                "collect_ths_concepts_api", "collect_intraday_stock_flow_market",
                "collect_intraday_sector_flow_full",
            ],
        )
