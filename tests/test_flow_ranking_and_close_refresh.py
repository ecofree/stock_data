"""Canonical flow ranking + close signal refresh after TuShare lag."""

from __future__ import annotations

import duckdb

from trade_system.flow_ranking import is_mega_sector_name, stock_provider_rank
from trade_system.normalize import build_normalized_views
from legacy_diagnostics import generate_stage_signals, refresh_close_signals_if_needed
from trade_system.web_report import render_dashboard_html


def test_stock_provider_prefers_full_market_over_kpl():
    assert stock_provider_rank("eastmoney_intraday_clist_delay") > stock_provider_rank("kpl")
    assert stock_provider_rank("tushare") > stock_provider_rank("kpl")


def test_mega_sector_filter():
    assert is_mega_sector_name("融资融券")
    assert is_mega_sector_name("深股通")
    assert not is_mega_sector_name("商业航天")


def test_close_refresh_fills_missing_price_after_tushare(tmp_path):
    db = tmp_path / "close.duckdb"
    trade_date = "2026-07-30"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, "
        "fall_count INTEGER, consecutive_count INTEGER, fetched_at TIMESTAMP, source_kind VARCHAR)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES (?,80,5,2800,2000,4,? || ' 15:05:00','real')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE stock_candidate_score("
        "trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, "
        "evidence_json VARCHAR, source VARCHAR)"
    )
    con.execute(
        "INSERT INTO stock_candidate_score VALUES (?,?,?,?,?,?)",
        [trade_date, "000001", "测试股", 80.0, "{}", "limit_pool"],
    )
    con.execute(
        "CREATE TABLE multi_source_stock_flow("
        "source_date DATE, stock_code VARCHAR, main_net DOUBLE, super_net DOUBLE, "
        "large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE, close DOUBLE, change_pct DOUBLE, "
        "turnover DOUBLE, provider VARCHAR, fetched_at TIMESTAMP, is_stale BOOLEAN)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "(?, '000001', 1e8, 0, 0, 0, 0, 10.5, 5.0, 1e7, "
        "'eastmoney_intraday_clist_delay', ? || ' 15:00:00', false)",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE multi_source_sector_flow("
        "source_date DATE, sector_code VARCHAR, sector_name VARCHAR, main_net DOUBLE, "
        "super_net DOUBLE, large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE, "
        "change_pct DOUBLE, main_ratio DOUBLE, "
        "provider VARCHAR, sector_type VARCHAR, amount_unit VARCHAR, "
        "fetched_at TIMESTAMP, is_stale BOOLEAN)"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES "
        "(?, 'BK001', '商业航天', 5e7, 1e7, 0, 0, 0, 2.5, 0.1, "
        "'eastmoney', 'em_industry', 'yuan', ? || ' 15:00:00', false)",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE sector_capital("
        "date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, "
        "big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_capital VALUES (?, 'BK001', 5e7, 0, 0, 0, 0, ? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE l2_realtime_all_boards("
        "date DATE, board_level INTEGER, stock_code VARCHAR, stock_name VARCHAR, "
        "limit_up_time VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_realtime_all_boards VALUES (?, 1, '000001', '测试股', '09:35', ? || ' 15:00:00')",
        [trade_date, trade_date],
    )
    # No kline / tushare yet — first close pass must be pending_provider.
    con.close()
    build_normalized_views(str(db))

    first = generate_stage_signals(
        str(db),
        trade_date,
        "close_decision",
        as_of_time=f"{trade_date}T17:30:00",
        run_id="first",
        limit=10,
        freshness_seconds=None,
        strict_tradability=True,
    )
    con = duckdb.connect(str(db), read_only=True)
    row = con.execute(
        "SELECT decision, reference_price, is_actionable, is_executable "
        "FROM stock_candidate_stage_signal WHERE stage='close_decision'"
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] == "pending_provider"
    assert row[1] is None
    assert row[2] is False

    # TuShare fills later.
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE tushare_daily("
        "ts_code VARCHAR, stock_code VARCHAR, date DATE, open DOUBLE, high DOUBLE, "
        "low DOUBLE, close DOUBLE, volume DOUBLE, turnover DOUBLE, change_pct DOUBLE, "
        "fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO tushare_daily VALUES "
        "('000001.SZ','000001',?,10,11,9.5,10.5,1e6,1e7,5.0,? || ' 19:00:00')",
        [trade_date, trade_date],
    )
    # A historical recovery may already have rerun the normal close generator,
    # replacing pending_provider with a cutoff-blocked row that now has a price.
    # The explicit late-refresh tool must still recognise and repair that state.
    con.execute(
        "UPDATE stock_candidate_stage_signal SET "
        "decision='blocked_data_quality',reference_price=10.5,is_actionable=false,"
        "evidence_json='{\"source_cutoff_ok\":false}' "
        "WHERE stage='close_decision'"
    )
    con.close()
    build_normalized_views(str(db))

    refreshed = refresh_close_signals_if_needed(
        str(db),
        trade_date,
        run_id="refresh",
        limit=10,
        as_of_time=f"{trade_date}T19:05:00",
    )
    assert refreshed.get("refreshed") is True
    con = duckdb.connect(str(db), read_only=True)
    row = con.execute(
        "SELECT decision, reference_price, is_actionable, is_executable "
        "FROM stock_candidate_stage_signal WHERE stage='close_decision'"
    ).fetchone()
    con.close()
    assert row[0] == "keep"
    assert float(row[1]) == 10.5
    assert row[2] is True
    # Close is review-only; not same-session entry.
    assert row[3] is False
    assert first["inserted"] >= 1


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
