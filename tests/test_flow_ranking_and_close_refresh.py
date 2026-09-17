"""Flow source ranking and historical dashboard reads, without signal writes."""

from __future__ import annotations

from trade_system.flow_ranking import is_mega_sector_name, stock_provider_rank
from trade_system.web_report import render_dashboard_html


def test_stock_provider_prefers_full_market_over_kpl():
    assert stock_provider_rank("eastmoney_intraday_clist_delay") > stock_provider_rank("kpl")
    assert stock_provider_rank("tushare") > stock_provider_rank("kpl")


def test_mega_sector_filter():
    assert is_mega_sector_name("融资融券")
    assert is_mega_sector_name("深股通")
    assert not is_mega_sector_name("商业航天")


def test_dashboard_renders_bool_pill_not_python_literal():
    html = render_dashboard_html(
        {
            "generated_at": "2026-07-30 18:00:00",
            "trade_date": "2026-07-30",
            "market": {"regime": "退潮", "suggested_position_pct": 5, "regime_score": 15},
            "execution": {
                "execution_ready": False,
                "actionable_candidates": 0,
                "block_reasons": [{"reason": "pending_provider", "count": 3}],
            },
            "data_chains": [],
            "counts": {},
            "count_details": {
                "kline": {
                    "relation": "kline",
                    "total": 24611,
                    "same_date": 0,
                    "latest": "2026-07-14",
                }
            },
            "sources": {},
            "stage_stats": {},
            "sectors": [],
            "candidates": [],
            "alerts": [],
            "reports": [],
            "gaps": [],
            "operator_candidates": [],
            "operator_origin_stats": [],
            "strategy_candidates": [],
            "auction_evidence": [
                {
                    "trade_date": "2026-07-30",
                    "stock_code": "000001",
                    "source_table": "auction_quote_snapshot",
                    "confirmation": "quote_confirmed",
                    "auction_strength": 90,
                    "is_fallback": False,
                    "missing_reason": "",
                }
            ],
            "research_context": [],
            "strategy_backtest": [],
            "qlib_shadow": [],
            "capital_flow": {
                "stock": [],
                "sector": [],
                "stock_top": [],
                "stock_bottom": [],
                "sector_top": [],
                "sector_bottom": [],
                "batch": {},
                "sector_batch": {},
                "candidate_pool": {},
            },
            "concept_status": {},
            "outcome_status": {},
            "qlib_status": {"signal_impact": "disabled"},
            "api_utilization": {},
        }
    )
    assert "False" not in html
    assert "True" not in html or "True" not in html.split("auction")[0]
    assert "real" in html or "fallback" in html
    assert "同日" in html
    assert "pending_provider" in html
