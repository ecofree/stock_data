"""Static HTML dashboard for inspecting the professional trading assistant state."""

from __future__ import annotations

from datetime import datetime, time
from html import escape
from pathlib import Path
from typing import Any

import duckdb

from trade_system.backtest import run_stage_candidate_backtest
from trade_system.data_chain import assess_data_chains
from trade_system.flow_ranking import sector_flow_rank_sql, stock_flow_rank_sql
from trade_system.i18n_labels import CATEGORY_CN, PROVIDER_CN, SEVERITY_CN, cn, zh_text
from trade_system.quality import table_columns, table_exists
from trade_system.readiness import assess_trade_date_readiness
from trade_system.db_utils import fetch_dicts as _fetch_dicts

try:
    from base import connect_duckdb
except Exception:  # pragma: no cover
    connect_duckdb = None  # type: ignore


COUNT_RELATIONS = [
    "auction_bidding_anomaly",
    "auction_tick",
    "advanced_morning_bidding_summary",
    "sector_capital",
    "kline",
    "v_kline_daily",
    "tushare_trade_cal",
    "tushare_stock_basic",
    "tushare_daily",
    "tushare_daily_basic",
    "tushare_adj_factor",
    "tushare_index_daily",
    "index_list",
    "index_kline",
    "etf_all",
    "finance_summary",
    "finance_income",
    "finance_balance",
    "finance_cashflow",
    "finance_fetch_checkpoint",
    "l2_realtime_index_list",
    "l2_stock_intraday",
    "l2_stock_bigorder",
    "l2_tick_history",
    "l2_tick_orders",
    "l2_tick_orders_all",
    "advanced_pankou",
    "advanced_main_monitor",
    "advanced_zjmm_min",
    "advanced_dadan_kline",
    "advanced_main_activity_kline",
    "v_intraday_capital_flow_evidence",
    "v_intraday_strength_evidence",
    "v_theme_mainline_evidence",
    "v_research_event_evidence",
    "v_lhb_review_evidence",
    "api_endpoint_inventory",
    "news_radar_item",
    "stock_candidate_score",
    "stock_candidate_stage_signal",
    "operator_trade_outcome",
    "qlib_stock_flow_features",
    "qlib_sector_flow_features",
    "qlib_model_registry",
    "qlib_prediction",
    "qlib_shadow_evaluation",
    "v_operator_candidates",
    "alert_events",
    "risk_snapshot",
]

SOURCE_VIEWS = [
    "v_auction_status",
    "v_sector_capital",
    "v_kline_daily",
    "v_index_state",
    "v_intraday_capital_flow_evidence",
    "v_intraday_strength_evidence",
    "v_theme_mainline_evidence",
    "v_research_event_evidence",
    "v_lhb_review_evidence",
]



def _safe_count(con: duckdb.DuckDBPyConnection, relation_name: str) -> int:
    try:
        return int(con.execute(f'SELECT count(*) FROM "{relation_name}"').fetchone()[0])
    except Exception:
        return 0


def _safe_same_date_count(
    con: duckdb.DuckDBPyConnection,
    relation_name: str,
    trade_date: str | None,
) -> dict[str, Any]:
    """Return total + same-date row counts and the latest date for a relation."""
    total = _safe_count(con, relation_name)
    result: dict[str, Any] = {
        "relation": relation_name,
        "total": total,
        "same_date": None,
        "latest": "",
    }
    if not trade_date or total <= 0:
        # still try latest
        try:
            cols = {
                row[1]
                for row in con.execute(f'PRAGMA table_info("{relation_name}")').fetchall()
            }
        except Exception:
            cols = set()
        date_col = next(
            (c for c in ("trade_date", "date", "source_date") if c in cols), None
        )
        if date_col:
            try:
                latest = con.execute(
                    f'SELECT max(CAST("{date_col}" AS VARCHAR)) FROM "{relation_name}"'
                ).fetchone()[0]
                result["latest"] = str(latest)[:10] if latest else ""
            except Exception:
                pass
        return result
    try:
        cols = {
            row[1]
            for row in con.execute(f'PRAGMA table_info("{relation_name}")').fetchall()
        }
    except Exception:
        # views may not support pragma table_info the same way
        cols = set()
        try:
            sample = con.execute(f'SELECT * FROM "{relation_name}" LIMIT 0')
            cols = {d[0] for d in sample.description}
        except Exception:
            return result
    date_col = next((c for c in ("trade_date", "date", "source_date") if c in cols), None)
    if not date_col:
        return result
    try:
        same = con.execute(
            f'SELECT count(*) FROM "{relation_name}" WHERE CAST("{date_col}" AS VARCHAR)=?',
            [str(trade_date)[:10]],
        ).fetchone()[0]
        latest = con.execute(
            f'SELECT max(CAST("{date_col}" AS VARCHAR)) FROM "{relation_name}"'
        ).fetchone()[0]
        result["same_date"] = int(same or 0)
        result["latest"] = str(latest)[:10] if latest else ""
    except Exception:
        pass
    return result


def _source_rows(con: duckdb.DuckDBPyConnection, view_name: str) -> list[dict]:
    try:
        cols = [row[1] for row in con.execute(f'PRAGMA table_info("{view_name}")').fetchall()]
        source_expr = "source_table" if "source_table" in cols else ("source_tables" if "source_tables" in cols else "'unknown'")
        fallback_expr = "is_fallback" if "is_fallback" in cols else "false"
        return _fetch_dicts(
            con,
            f"""
            SELECT {source_expr} AS source_table, {fallback_expr} AS is_fallback, count(*) AS count
            FROM "{view_name}"
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
        )
    except Exception:
        return []


def _api_utilization(con: duckdb.DuckDBPyConnection) -> dict:
    if not table_exists(con, "api_endpoint_inventory"):
        return {
            "discovered_endpoint_count": 0,
            "available_endpoint_count": 0,
            "landed_endpoint_count": 0,
            "signal_endpoint_count": 0,
            "high_value_unused_count": 0,
            "workflow_evidence": {},
        }
    rows = _fetch_dicts(
        con,
        """
        SELECT endpoint, table_name, table_rows, verdict, usefulness
        FROM api_endpoint_inventory
        ORDER BY endpoint
        """,
    )
    available = {"stable_available", "api_available"}
    signal_tables = {
        "daily_summary",
        "market_rise_fall",
        "market_emotion_money",
        "sector_strength",
        "sector_capital",
        "sector_all_stocks",
        "sector_boom_reason",
        "kline",
        "index_kline",
        "index_intraday",
        "l2_realtime_index_list",
        "l2_realtime_index_trend",
        "auction_tick",
        "auction_bidding_anomaly",
        "advanced_morning_bidding_summary",
        "l2_realtime_all_boards",
        "sector_stocks",
        "l2_stock_intraday",
        "l2_stock_bigorder",
        "l2_tick_history",
        "l2_tick_orders",
        "l2_tick_orders_all",
        "advanced_pankou",
        "advanced_main_monitor",
        "advanced_zjmm_min",
        "advanced_dadan_kline",
        "advanced_main_activity_kline",
    }

    def endpoints_for(table_names: set[str]) -> list[str]:
        return [
            row["endpoint"]
            for row in rows
            if row.get("table_name") in table_names and int(row.get("table_rows") or 0) > 0
        ][:12]

    workflow = {
        "pre_market": endpoints_for({"daily_summary", "market_rise_fall", "sector_strength", "sector_capital", "kline"}),
        "auction": endpoints_for({"auction_tick", "auction_bidding_anomaly", "advanced_morning_bidding_summary"}),
        "intraday": endpoints_for({
            "l2_stock_intraday",
            "l2_stock_bigorder",
            "l2_tick_history",
            "l2_tick_orders",
            "l2_tick_orders_all",
            "advanced_pankou",
            "advanced_main_monitor",
            "advanced_zjmm_min",
            "advanced_dadan_kline",
            "advanced_main_activity_kline",
        }),
        "close": endpoints_for({"kline", "index_kline", "index_intraday", "l2_realtime_index_list"}),
        "post_market": endpoints_for({"advanced_news_flash", "news_theme", "news_plate", "lhb_list", "lhb_detail", "lhb_youzi_dongxiang"}),
    }
    landed = [row for row in rows if int(row.get("table_rows") or 0) > 0]
    return {
        "discovered_endpoint_count": len(rows),
        "available_endpoint_count": sum(1 for row in rows if row.get("verdict") in available),
        "landed_endpoint_count": len(landed),
        "signal_endpoint_count": sum(1 for row in landed if row.get("table_name") in signal_tables),
        "high_value_unused_count": sum(
            1
            for row in rows
            if row.get("verdict") in available
            and int(row.get("table_rows") or 0) == 0
            and str(row.get("usefulness") or "").startswith("professional")
        ),
        "workflow_evidence": workflow,
    }


def _latest_market(con: duckdb.DuckDBPyConnection, trade_date: str | None = None) -> dict:
    if not table_exists(con, "market_regime_snapshot"):
        return {}
    query = f"""
        SELECT trade_date, regime, suggested_position_pct, regime_score, generated_at
        FROM market_regime_snapshot
        {"WHERE trade_date = ?" if trade_date else ""}
        ORDER BY generated_at DESC NULLS LAST
        LIMIT 1
        """
    rows = _fetch_dicts(con, query, [trade_date] if trade_date else [])
    return rows[0] if rows else {}


def _stage_stats(db_path: str | Path, con: duckdb.DuckDBPyConnection) -> dict:
    try:
        return run_stage_candidate_backtest(db_path, enforce_t1=True).get("stage_stats", {})
    except Exception:
        if not table_exists(con, "stock_candidate_stage_signal"):
            return {}
        rows = con.execute(
            """
            SELECT stage, count(*) AS sample_count
            FROM stock_candidate_stage_signal
            GROUP BY stage
            ORDER BY stage
            """
        ).fetchall()
        return {
            stage: {
                "sample_count": int(count),
                "return_sample_count": 0,
                "hit_rate": None,
                "avg_forward_return_pct": None,
            }
            for stage, count in rows
        }


def _latest_rows(con: duckdb.DuckDBPyConnection, table_name: str, order_column: str, limit: int = 10) -> list[dict]:
    if not table_exists(con, table_name):
        return []
    try:
        return _fetch_dicts(con, f'SELECT * FROM "{table_name}" ORDER BY "{order_column}" DESC LIMIT {int(limit)}')
    except Exception:
        return []


def _operator_candidates(
    con: duckdb.DuckDBPyConnection,
    limit: int = 30,
    trade_date: str | None = None,
) -> list[dict]:
    try:
        if trade_date:
            return _fetch_dicts(
                con,
                f"""
                SELECT trade_date, stage, stock_code, stock_name, score, decision, data_origin
                FROM v_operator_candidates
                WHERE CAST(trade_date AS VARCHAR)=?
                ORDER BY score DESC NULLS LAST, stock_code
                LIMIT {int(limit)}
                """,
                [str(trade_date)[:10]],
            )
        return _fetch_dicts(
            con,
            f"""
            SELECT trade_date, stage, stock_code, stock_name, score, decision, data_origin
            FROM v_operator_candidates
            ORDER BY trade_date DESC NULLS LAST, score DESC NULLS LAST, stock_code
            LIMIT {int(limit)}
            """,
        )
    except Exception:
        return []


def _operator_origin_stats(
    con: duckdb.DuckDBPyConnection,
    trade_date: str | None = None,
) -> list[dict]:
    try:
        if trade_date:
            return _fetch_dicts(
                con,
                """
                SELECT data_origin, count(*) AS count
                FROM v_operator_candidates
                WHERE CAST(trade_date AS VARCHAR)=?
                GROUP BY 1
                ORDER BY 1
                """,
                [str(trade_date)[:10]],
            )
        return _fetch_dicts(
            con,
            """
            SELECT data_origin, count(*) AS count
            FROM v_operator_candidates
            GROUP BY 1
            ORDER BY 1
            """,
        )
    except Exception:
        return []


def _strategy_candidates(
    con: duckdb.DuckDBPyConnection,
    limit: int = 30,
    trade_date: str | None = None,
) -> list[dict]:
    try:
        if trade_date:
            return _fetch_dicts(
                con,
                f"""
                SELECT trade_date, stage, symbol, stock_name, score, selected_reason, risk_points, invalid_conditions
                FROM strategy_scan_result
                WHERE CAST(trade_date AS VARCHAR)=?
                ORDER BY score DESC NULLS LAST, symbol
                LIMIT {int(limit)}
                """,
                [str(trade_date)[:10]],
            )
        return _fetch_dicts(
            con,
            f"""
            SELECT trade_date, stage, symbol, stock_name, score, selected_reason, risk_points, invalid_conditions
            FROM strategy_scan_result
            ORDER BY trade_date DESC NULLS LAST, score DESC NULLS LAST, symbol
            LIMIT {int(limit)}
            """,
        )
    except Exception:
        return []


def _research_context(
    con: duckdb.DuckDBPyConnection,
    limit: int = 20,
    trade_date: str | None = None,
) -> list[dict]:
    try:
        if trade_date:
            return _fetch_dicts(
                con,
                f"""
                SELECT context_type, context_date, symbol, sector, title, summary, ref_id
                FROM v_operator_research_context
                WHERE CAST(context_date AS VARCHAR)=?
                  AND coalesce(title, '') NOT LIKE '%interview count 0%'
                ORDER BY context_type, title
                LIMIT {int(limit)}
                """,
                [str(trade_date)[:10]],
            )
        return _fetch_dicts(
            con,
            f"""
            SELECT context_type, context_date, symbol, sector, title, summary, ref_id
            FROM v_operator_research_context
            WHERE coalesce(title, '') NOT LIKE '%interview count 0%'
            ORDER BY context_date DESC NULLS LAST, context_type, title
            LIMIT {int(limit)}
            """,
        )
    except Exception:
        return []


def _strategy_backtest_rows(con: duckdb.DuckDBPyConnection, limit: int = 20) -> list[dict]:
    if not table_exists(con, "strategy_backtest_result"):
        return []
    try:
        return _fetch_dicts(
            con,
            f"""
            SELECT strategy_id, stage, sample_count, win_rate, avg_return
            FROM strategy_backtest_result
            ORDER BY sample_count DESC, strategy_id
            LIMIT {int(limit)}
            """,
        )
    except Exception:
        return []


def _qlib_shadow_rows(con: duckdb.DuckDBPyConnection, limit: int = 20) -> list[dict]:
    if not table_exists(con, "qlib_shadow_evaluation"):
        return []
    try:
        columns = set(table_columns(con, "qlib_shadow_evaluation"))
        optional = [
            "ic", "rank_ic", "avg_forward_return", "top_bottom_spread", "max_drawdown",
        ]
        select_optional = [column if column in columns else f"NULL AS {column}" for column in optional]
        return _fetch_dicts(
            con,
            f"""
            SELECT model_id, sample_count, hit_rate, top_quantile_return, bottom_quantile_return,
                   {', '.join(select_optional)}
            FROM qlib_shadow_evaluation
            ORDER BY sample_count DESC, model_id
            LIMIT {int(limit)}
            """,
        )
    except Exception:
        return []


def _latest_relation_date(con: duckdb.DuckDBPyConnection, relation: str, date_column: str) -> str:
    if not table_exists(con, relation):
        return ""
    try:
        value = con.execute(f'SELECT max("{date_column}") FROM "{relation}"').fetchone()[0]
        return str(value)[:10] if value else ""
    except Exception:
        return ""


def _capital_flow_snapshot(
    con: duckdb.DuckDBPyConnection,
    trade_date: str | None = None,
) -> dict[str, Any]:
    """Return a compact, source-aware snapshot for the operator dashboard.

    Rankings use one canonical row per code (provider priority) and exclude
    mega/index membership baskets that drown out tradeable themes.
    """
    stock = []
    sector = []
    # Resolve the effective flow date: prefer the requested trade date.
    effective_date = str(trade_date)[:10] if trade_date else ""
    if not effective_date and table_exists(con, "multi_source_stock_flow"):
        try:
            effective_date = str(
                con.execute("SELECT max(source_date) FROM multi_source_stock_flow").fetchone()[0] or ""
            )[:10]
        except Exception:
            effective_date = ""

    for relation, date_column, code_column in (
        ("multi_source_stock_flow", "source_date", "stock_code"),
        ("advanced_zjmm_min", "date", "stock_code"),
    ):
        if not table_exists(con, relation):
            continue
        try:
            if effective_date:
                row = con.execute(
                    f"SELECT max({date_column}), count(*), count(DISTINCT {code_column}) "
                    f"FROM {relation} WHERE CAST({date_column} AS VARCHAR)=?",
                    [effective_date],
                ).fetchone()
            else:
                row = con.execute(
                    f"SELECT max({date_column}), count(*), count(DISTINCT {code_column}) "
                    f"FROM {relation} WHERE {date_column}=(SELECT max({date_column}) FROM {relation})"
                ).fetchone()
            stock.append(
                {
                    "relation": relation,
                    "latest": str(row[0])[:10] if row[0] else "",
                    "rows": int(row[1] or 0),
                    "codes": int(row[2] or 0),
                }
            )
        except Exception:
            continue
    for relation, date_column, code_column in (
        ("multi_source_sector_flow", "source_date", "sector_code"),
        ("sector_capital", "date", "sector_code"),
    ):
        if not table_exists(con, relation):
            continue
        try:
            if effective_date:
                row = con.execute(
                    f"SELECT max({date_column}), count(*), count(DISTINCT {code_column}) "
                    f"FROM {relation} WHERE CAST({date_column} AS VARCHAR)=?",
                    [effective_date],
                ).fetchone()
            else:
                row = con.execute(
                    f"SELECT max({date_column}), count(*), count(DISTINCT {code_column}) "
                    f"FROM {relation} WHERE {date_column}=(SELECT max({date_column}) FROM {relation})"
                ).fetchone()
            sector.append(
                {
                    "relation": relation,
                    "latest": str(row[0])[:10] if row[0] else "",
                    "rows": int(row[1] or 0),
                    "codes": int(row[2] or 0),
                }
            )
        except Exception:
            continue

    stock_top: list[dict] = []
    stock_bottom: list[dict] = []
    if table_exists(con, "multi_source_stock_flow") and effective_date:
        try:
            ranked = stock_flow_rank_sql()
            stock_top = _fetch_dicts(
                con,
                f"""
                SELECT source_date, stock_code, main_net, provider, fetched_at, is_stale
                FROM ({ranked})
                ORDER BY main_net DESC NULLS LAST
                LIMIT 20
                """,
                [effective_date],
            )
            stock_bottom = _fetch_dicts(
                con,
                f"""
                SELECT source_date, stock_code, main_net, provider, fetched_at, is_stale
                FROM ({ranked})
                ORDER BY main_net ASC NULLS LAST
                LIMIT 20
                """,
                [effective_date],
            )
        except Exception:
            stock_top = []
            stock_bottom = []
    sector_top: list[dict] = []
    sector_bottom: list[dict] = []
    if table_exists(con, "multi_source_sector_flow") and effective_date:
        try:
            ranked = sector_flow_rank_sql(exclude_mega=True)
            sector_top = _fetch_dicts(
                con,
                f"""
                SELECT source_date, sector_code, sector_name, main_net, provider, fetched_at, is_stale
                FROM ({ranked})
                ORDER BY main_net DESC NULLS LAST
                LIMIT 20
                """,
                [effective_date],
            )
            sector_bottom = _fetch_dicts(
                con,
                f"""
                SELECT source_date, sector_code, sector_name, main_net, provider, fetched_at, is_stale
                FROM ({ranked})
                ORDER BY main_net ASC NULLS LAST
                LIMIT 20
                """,
                [effective_date],
            )
        except Exception:
            sector_top = []
            sector_bottom = []
    batch = {}
    if table_exists(con, "intraday_stock_flow_batch"):
        try:
            if effective_date:
                row = con.execute(
                    "SELECT trade_date,provider,expected_rows,fetched_rows,coverage_pct,status,updated_at,last_error "
                    "FROM intraday_stock_flow_batch WHERE CAST(trade_date AS VARCHAR)=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    [effective_date],
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT trade_date,provider,expected_rows,fetched_rows,coverage_pct,status,updated_at,last_error "
                    "FROM intraday_stock_flow_batch ORDER BY trade_date DESC LIMIT 1"
                ).fetchone()
            if row:
                batch = {
                    "trade_date": str(row[0])[:10],
                    "provider": row[1],
                    "expected_rows": int(row[2] or 0),
                    "fetched_rows": int(row[3] or 0),
                    "coverage_pct": float(row[4] or 0),
                    "status": row[5],
                    "updated_at": str(row[6]) if row[6] else "",
                    "last_error": row[7] or "",
                }
        except Exception:
            batch = {}
    sector_batch = {}
    if table_exists(con, "intraday_sector_flow_batch"):
        try:
            if effective_date:
                row = con.execute(
                    "SELECT trade_date,provider,expected_rows,fetched_rows,coverage_pct,status,updated_at,last_error "
                    "FROM intraday_sector_flow_batch WHERE CAST(trade_date AS VARCHAR)=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    [effective_date],
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT trade_date,provider,expected_rows,fetched_rows,coverage_pct,status,updated_at,last_error "
                    "FROM intraday_sector_flow_batch ORDER BY trade_date DESC LIMIT 1"
                ).fetchone()
            if row:
                sector_batch = {
                    "trade_date": str(row[0])[:10],
                    "provider": row[1],
                    "expected_rows": int(row[2] or 0),
                    "fetched_rows": int(row[3] or 0),
                    "coverage_pct": float(row[4] or 0),
                    "status": row[5],
                    "updated_at": str(row[6]) if row[6] else "",
                    "last_error": row[7] or "",
                }
        except Exception:
            sector_batch = {}
    candidate_pool = {}
    if table_exists(con, "realtime_candidate_pool_snapshot"):
        try:
            if effective_date:
                row = con.execute(
                    "SELECT trade_date,source,row_count,stock_count,status,fetched_at,error "
                    "FROM realtime_candidate_pool_snapshot WHERE CAST(trade_date AS VARCHAR)=? "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    [effective_date],
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT trade_date,source,row_count,stock_count,status,fetched_at,error "
                    "FROM realtime_candidate_pool_snapshot ORDER BY trade_date DESC LIMIT 1"
                ).fetchone()
            if row:
                candidate_pool = {
                    "trade_date": str(row[0])[:10],
                    "source": row[1],
                    "row_count": int(row[2] or 0),
                    "stock_count": int(row[3] or 0),
                    "status": row[4],
                    "fetched_at": str(row[5]) if row[5] else "",
                    "error": row[6] or "",
                }
        except Exception:
            candidate_pool = {}
    return {
        "stock": stock,
        "sector": sector,
        "stock_top": stock_top,
        "stock_bottom": stock_bottom,
        "sector_top": sector_top,
        "sector_bottom": sector_bottom,
        "batch": batch,
        "sector_batch": sector_batch,
        "candidate_pool": candidate_pool,
        "effective_date": effective_date,
    }


def _concept_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    # The production page is a current-snapshot view.  Counting every THS
    # snapshot ever stored here made the dashboard show 1,859 concepts and
    # 326k member rows for a 375-concept / 63k-member current snapshot.
    result = {
        "concepts": 0,
        "members": 0,
        "verified": 0,
        "partial": 0,
        "latest": "",
        "snapshot_status": "missing",
        "history_concepts": 0,
        "history_members": 0,
        "top_partial": [],
    }
    if table_exists(con, "ths_concept_daily"):
        row = con.execute(
            "SELECT count(*), sum(CASE WHEN date_verified THEN 1 ELSE 0 END), max(trade_date) "
            "FROM ths_concept_daily"
        ).fetchone()
        latest = str(row[2])[:10] if row[2] else ""
        result["latest"] = latest
        result["history_concepts"] = int(row[0] or 0)
        if latest:
            current = con.execute(
                "SELECT count(*), sum(CASE WHEN date_verified THEN 1 ELSE 0 END) "
                "FROM ths_concept_daily WHERE trade_date=CAST(? AS DATE)",
                [latest],
            ).fetchone()
            result["concepts"] = int(current[0] or 0)
            result["verified"] = int(current[1] or 0)
    if table_exists(con, "ths_concept_stock_history"):
        result["history_members"] = int(
            con.execute("SELECT count(*) FROM ths_concept_stock_history").fetchone()[0]
        )
        if result["latest"]:
            result["members"] = int(
                con.execute(
                    "SELECT count(*) FROM ths_concept_stock_history "
                    "WHERE trade_date=CAST(? AS DATE)",
                    [result["latest"]],
                ).fetchone()[0]
            )
    if table_exists(con, "ths_concept_member_checkpoint") and result["latest"]:
        result["verified"] = int(
            con.execute(
                "SELECT count(*) FROM ths_concept_member_checkpoint "
                "WHERE trade_date=CAST(? AS DATE) AND status='success'",
                [result["latest"]],
            ).fetchone()[0]
        )
        result["partial"] = int(
            con.execute(
                "SELECT count(*) FROM ths_concept_member_checkpoint "
                "WHERE trade_date=CAST(? AS DATE) AND status <> 'success'",
                [result["latest"]],
            ).fetchone()[0]
        )
        result["snapshot_status"] = "success" if result["partial"] == 0 else "partial"
        result["top_partial"] = _fetch_dicts(
            con,
            """
            SELECT concept_code, concept_name, status, pages_expected, pages_fetched, member_rows, last_error
            FROM ths_concept_member_checkpoint
            WHERE trade_date=CAST(? AS DATE) AND status <> 'success'
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            [result["latest"]],
        )
    return result


def _outcome_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    result = {"plans": 0, "journals": 0, "outcomes": 0, "executed": 0, "skipped": 0, "cancelled": 0,
              "review_required": 0, "latest_date": ""}
    for key, relation in (("plans", "trade_plan"), ("journals", "trade_journal"), ("outcomes", "operator_trade_outcome")):
        if table_exists(con, relation):
            result[key] = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
    if table_exists(con, "operator_trade_outcome"):
        result["latest_date"] = str(con.execute(
            "SELECT max(trade_date) FROM operator_trade_outcome"
        ).fetchone()[0] or "")[:10]
        rows = con.execute(
            "SELECT lower(coalesce(execution_status, '')) AS status, count(*) FROM operator_trade_outcome GROUP BY 1"
        ).fetchall()
        for status, count in rows:
            if status in result:
                result[status] = int(count)
    return result


def _qlib_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    result = {"models": 0, "predictions": 0, "evaluations": 0, "latest_sample": 0, "overlap": 0, "signal_impact": "disabled"}
    for key, relation in (("models", "qlib_model_registry"), ("predictions", "qlib_prediction"), ("evaluations", "qlib_shadow_evaluation")):
        if table_exists(con, relation):
            result[key] = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
    if table_exists(con, "qlib_shadow_evaluation"):
        result["latest_sample"] = int(con.execute(
            "SELECT coalesce(sum(sample_count), 0) FROM qlib_shadow_evaluation"
        ).fetchone()[0])
    if table_exists(con, "v_qlib_shadow_candidate_overlap"):
        result["overlap"] = int(con.execute(
            "SELECT count(*) FROM v_qlib_shadow_candidate_overlap WHERE strategy_id IS NOT NULL"
        ).fetchone()[0])
    return result


def _auction_evidence_rows(
    con: duckdb.DuckDBPyConnection,
    limit: int = 20,
    trade_date: str | None = None,
) -> list[dict]:
    if not table_exists(con, "auction_evidence_snapshot"):
        return []
    try:
        if trade_date:
            return _fetch_dicts(
                con,
                f"""
                SELECT trade_date, stock_code, source_table, confirmation, auction_strength, is_fallback, missing_reason
                FROM auction_evidence_snapshot
                WHERE CAST(trade_date AS VARCHAR)=?
                ORDER BY is_fallback, auction_strength DESC NULLS LAST
                LIMIT {int(limit)}
                """,
                [str(trade_date)[:10]],
            )
        return _fetch_dicts(
            con,
            f"""
            SELECT trade_date, stock_code, source_table, confirmation, auction_strength, is_fallback, missing_reason
            FROM auction_evidence_snapshot
            ORDER BY trade_date DESC, is_fallback, auction_strength DESC NULLS LAST
            LIMIT {int(limit)}
            """,
        )
    except Exception:
        return []


def _report_files(reports_dir: str | Path) -> list[dict]:
    root = Path(reports_dir)
    if not root.exists():
        return []
    reports = []
    for path in sorted(root.glob("*latest.md")):
        reports.append({"name": path.name, "path": path.name, "size": path.stat().st_size})
    return reports


def _gaps(counts: dict[str, int], sources: dict[str, list[dict]], stage_stats: dict) -> list[str]:
    gaps = []
    if counts.get("auction_tick", 0) == 0:
        gaps.append("auction_tick is empty;竞价确认仍主要依赖竞价异常/竞价摘要。")
    if counts.get("risk_snapshot", 0) == 0:
        gaps.append("risk_snapshot is empty;真实持仓、计划和风控快照还没有形成闭环。")
    index_sources = sources.get("v_index_state", [])
    if counts.get("finance_summary", 0) == 0:
        gaps.append("finance_summary is empty; run the bounded collect_finance.py gap-fill and inspect its checkpoint table.")
    if counts.get("etf_all", 0) == 0:
        gaps.append("etf_all is empty; retain the raw upstream payload until ETF-code validation passes.")
    if counts.get("operator_trade_outcome", 0) == 0:
        gaps.append("operator_trade_outcome is empty; fill the generated operator outcome template before treating backtest results as live performance.")
    if not index_sources or any(row.get("is_fallback") for row in index_sources):
        gaps.append("index state is incomplete or fallback;指数历史/实时来源仍需补强。")
    return_samples = sum(int((stats or {}).get("return_sample_count") or 0) for stats in stage_stats.values())
    signal_samples = sum(int((stats or {}).get("sample_count") or 0) for stats in stage_stats.values())
    if signal_samples and return_samples < signal_samples:
        gaps.append(f"stage backtest return samples are limited: {return_samples}/{signal_samples}.")
    return gaps


def _execution_status(con, trade_date: str) -> dict:
    """P0#3: distinguish analytics-readiness from execution-readiness on the dashboard.

    A delayed-only snapshot can be fully present (analytics-ready) yet support no
    executable candidate; surface that explicitly instead of implying a green
    'ready to trade' state.

    ``actionable`` = stage evidence complete for the decision (incl. close keep/reduce).
    ``executable`` = same-session entry is allowed (``is_executable``). Close
    ``signal_close`` rows are actionable for review but never executable.
    """
    total = 0
    actionable = 0
    executable = 0
    block_reasons: list = []
    now = datetime.now()
    active_stage = None
    if str(trade_date)[:10] == now.date().isoformat():
        current = now.time()
        if time(9, 15) <= current < time(9, 30):
            active_stage = "auction_confirmation"
        elif (time(9, 30) <= current <= time(11, 30)) or (
            time(13, 0) <= current < time(14, 50)
        ):
            active_stage = "intraday_strength"
    try:
        if table_exists(con, "stock_candidate_stage_signal"):
            columns = table_columns(con, "stock_candidate_stage_signal")
            stage_filter = " AND stage=?" if active_stage else " AND 1=0"
            valid_filter = (
                " AND execution_valid_until IS NOT NULL AND execution_valid_until >= ?"
                if "execution_valid_until" in columns and active_stage
                else ""
            )
            params = [str(trade_date)[:10]]
            if active_stage:
                params.append(active_stage)
                if valid_filter:
                    params.append(now)
            row = con.execute(
                "SELECT count(*), "
                "sum(CASE WHEN coalesce(is_actionable,false) THEN 1 ELSE 0 END), "
                "sum(CASE WHEN coalesce(is_executable,false) THEN 1 ELSE 0 END) "
                "FROM stock_candidate_stage_signal WHERE CAST(trade_date AS VARCHAR)=?"
                + stage_filter + valid_filter,
                params,
            ).fetchone()
            total = int(row[0] or 0)
            actionable = int(row[1] or 0)
            executable = int(row[2] or 0)
            # Prefer entry-block reasons when present; fall back to row_block/decision.
            reason_rows = con.execute(
                "SELECT coalesce("
                "json_extract_string(evidence_json, '$.entry_block_reason'), "
                "json_extract_string(evidence_json, '$.row_block_reason'), "
                "decision) AS reason, "
                "count(*) AS n FROM stock_candidate_stage_signal "
                "WHERE CAST(trade_date AS VARCHAR)=? "
                + stage_filter + valid_filter + " "
                "AND coalesce(is_executable,false)=false "
                "GROUP BY 1 ORDER BY n DESC LIMIT 6",
                params,
            ).fetchall()
            block_reasons = [
                {"reason": str(r[0] or "unknown"), "count": int(r[1] or 0)}
                for r in reason_rows
            ]
    except Exception:
        pass
    return {
        "candidate_total": total,
        "actionable_candidates": actionable,
        "executable_candidates": executable,
        "execution_ready": executable > 0,
        "block_reasons": block_reasons,
    }


def load_dashboard_context(
    db_path: str | Path,
    reports_dir: str | Path = "reports",
    trade_date: str | None = None,
) -> dict:
    if connect_duckdb is not None:
        con = connect_duckdb(str(db_path), read_only=True)
    else:
        con = duckdb.connect(str(db_path), read_only=True)
    try:
        market = _latest_market(con, trade_date)
        effective_trade_date = trade_date or str(market.get("trade_date") or "")
        # Prefer same-date metrics so total historical bloat cannot look "healthy".
        count_details = {
            name: _safe_same_date_count(con, name, effective_trade_date or None)
            for name in COUNT_RELATIONS
        }
        counts = {
            name: (
                detail["same_date"]
                if detail.get("same_date") is not None
                else detail.get("total", 0)
            )
            for name, detail in count_details.items()
        }
        sources = {name: _source_rows(con, name) for name in SOURCE_VIEWS}
        sector_columns = set(table_columns(con, "sector_rotation_score"))
        sector_filter = (
            "AND (taxonomy IN ('ths_concept','ths_concept_derived') "
            "OR (coalesce(taxonomy,'unknown')='unknown' AND sector_code LIKE 'THS-%'))"
            if "taxonomy" in sector_columns else "AND sector_code LIKE 'THS-%'"
        )
        if effective_trade_date and table_exists(con, "sector_rotation_score"):
            try:
                sectors = _fetch_dicts(
                    con,
                    "SELECT * FROM sector_rotation_score "
                    "WHERE CAST(trade_date AS VARCHAR)=? "
                    + sector_filter
                    + " ORDER BY score DESC NULLS LAST LIMIT 10",
                    [str(effective_trade_date)[:10]],
                )
            except Exception:
                sectors = []
        else:
            sectors = []
        # Lock candidates to the page trade date when available.
        if effective_trade_date and table_exists(con, "stock_candidate_score"):
            try:
                candidates = _fetch_dicts(
                    con,
                    """
                    SELECT *
                    FROM stock_candidate_score
                    WHERE CAST(trade_date AS VARCHAR)=?
                    ORDER BY score DESC NULLS LAST
                    LIMIT 20
                    """,
                    [str(effective_trade_date)[:10]],
                )
            except Exception:
                candidates = _latest_rows(con, "stock_candidate_score", "score", 20)
        else:
            candidates = _latest_rows(con, "stock_candidate_score", "score", 20)
        operator_candidates = _operator_candidates(con, trade_date=effective_trade_date or None)
        operator_origin_stats = _operator_origin_stats(con, trade_date=effective_trade_date or None)
        strategy_candidates = _strategy_candidates(con, trade_date=effective_trade_date or None)
        research_context = _research_context(con, trade_date=effective_trade_date or None)
        strategy_backtest = _strategy_backtest_rows(con)
        qlib_shadow = _qlib_shadow_rows(con)
        capital_flow = _capital_flow_snapshot(con, trade_date=effective_trade_date or None)
        concept_status = _concept_status(con)
        outcome_status = _outcome_status(con)
        qlib_status = _qlib_status(con)
        auction_evidence = _auction_evidence_rows(con, trade_date=effective_trade_date or None)
        alerts = _latest_rows(con, "alert_events", "generated_at", 10)
        stage_stats = _stage_stats(db_path, con)
        api_utilization = _api_utilization(con)
        execution = _execution_status(con, effective_trade_date)
        try:
            readiness = assess_trade_date_readiness(con, effective_trade_date, stage="postmarket")
        except Exception as exc:
            readiness = {
                "source_ready": False, "pipeline_ready": False,
                "artifact_current": False, "certified_ready": False,
                "analytics_ready": False, "execution_ready": False,
                "missing_groups": [f"readiness_error:{type(exc).__name__}"],
                "actionable_candidates": 0, "tradable_candidates": 0,
                "risk_approved_candidates": 0, "executable_candidates": 0,
            }
    finally:
        con.close()

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": effective_trade_date,
        "requested_trade_date": trade_date,
        "market_state_date": str(market.get("trade_date") or ""),
        "date_consistent": bool(
            not trade_date or str(market.get("trade_date") or "") == trade_date
        ),
        "market": market,
        "execution": execution,
        "readiness": readiness,
        "data_chains": assess_data_chains(db_path, trade_date=effective_trade_date or None),
        "counts": counts,
        "count_details": count_details,
        "sources": sources,
        "stage_stats": stage_stats,
        "sectors": sectors,
        "candidates": candidates,
        "operator_candidates": operator_candidates,
        "operator_origin_stats": operator_origin_stats,
        "strategy_candidates": strategy_candidates,
        "research_context": research_context,
        "strategy_backtest": strategy_backtest,
        "qlib_shadow": qlib_shadow,
        "qlib_status": qlib_status,
        "capital_flow": capital_flow,
        "concept_status": concept_status,
        "outcome_status": outcome_status,
        "auction_evidence": auction_evidence,
        "api_utilization": api_utilization,
        "alerts": alerts,
        "reports": _report_files(reports_dir),
        "gaps": _gaps(counts, sources, stage_stats),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "是" if value else "否"
    return escape(str(value))


def _bool_pill(value: Any) -> str:
    truthy = value in (True, "true", "True", 1, "1", "是")
    if value in (False, "false", "False", 0, "0", "否"):
        truthy = False
    elif value in (None, ""):
        return "<span class='pill muted'>—</span>"
    label = "是" if truthy else "否"
    cls = "warn" if truthy else "ok"
    # Fallback evidence is a warning; real/non-fallback is ok.
    if isinstance(value, bool) or str(value).lower() in {"true", "false"}:
        # For is_fallback: True=warn, False=ok
        cls = "warn" if truthy else "ok"
        label = "fallback" if truthy else "real"
    return f"<span class='pill {cls}'>{escape(label)}</span>"


def _status_class(status: str) -> str:
    return {
        "available": "ok",
        "fallback": "warn",
        "missing": "bad",
        "stale": "warn",
    }.get(status, "muted")


def render_dashboard_html(context: dict) -> str:
    market = context.get("market", {})
    execution = context.get("execution", {})
    readiness = context.get("readiness", {})
    exec_ready = bool(execution.get("execution_ready"))
    exec_class = "ok" if exec_ready else "bad"
    exec_label = "是" if exec_ready else "否"
    exec_actionable = execution.get("actionable_candidates", 0)
    exec_executable = execution.get("executable_candidates", 0)
    exec_block_reasons = execution.get("block_reasons") or []
    source_ready = bool(readiness.get("source_ready", readiness.get("analytics_ready", False)))
    pipeline_ready = bool(readiness.get("pipeline_ready", source_ready))
    certified_ready = bool(readiness.get("certified_ready", False))
    readiness_class = "ok" if certified_ready else "warn" if pipeline_ready else "bad"
    readiness_label = "CERTIFIED" if certified_ready else "PIPELINE" if pipeline_ready else "SOURCE" if source_ready else "BLOCKED"
    readiness_missing = ", ".join(readiness.get("missing_groups") or []) or "none"
    exec_block_note = "; ".join(
        f"{item.get('reason')}×{item.get('count')}" for item in exec_block_reasons
    )
    requested_date = context.get("requested_trade_date")
    date_warning = ""
    if requested_date and not context.get("date_consistent", False):
        date_warning = (
            "<div class='alert bad'><strong>日期一致性失败：</strong>"
            f"请求交易日 {escape(str(requested_date))} 没有对应的市场状态快照"
            "，本页只用于诊断，禁止据此执行交易。</div>"
        )
    chain_rows = []
    for item in context.get("data_chains", []):
        present = ", ".join(item.get("required_present") or item.get("fallback_present") or [])
        missing = ", ".join(item.get("required_missing") or [])
        status = str(item.get("status", ""))
        chain_rows.append(
            f"<tr><td>{_fmt(item.get('chain'))}</td><td><span class='pill {_status_class(status)}'>{_fmt(status)}</span></td>"
            f"<td>{_fmt(present)}</td><td>{_fmt(missing)}</td></tr>"
        )

    count_details = context.get("count_details") or {}
    if count_details:
        count_cards = "\n".join(
            (
                f"<div class='metric'><span>{escape(name)}"
                f"{(' · ' + escape(str(detail.get('latest')))) if detail.get('latest') else ''}"
                f"</span><strong>"
                f"{detail.get('same_date') if detail.get('same_date') is not None else detail.get('total', 0)}"
                f"</strong>"
                f"<span class='muted'>同日"
                f"{'' if detail.get('same_date') is None else ''}"
                f" / 全量 {detail.get('total', 0)}</span></div>"
            )
            for name, detail in count_details.items()
        )
    else:
        count_cards = "\n".join(
            f"<div class='metric'><span>{escape(name)}</span><strong>{count}</strong></div>"
            for name, count in context.get("counts", {}).items()
        )
    api = context.get("api_utilization", {})
    api_cards = "\n".join(
        [
            f"<div class='metric'><span>Discovered endpoints</span><strong>{_fmt(api.get('discovered_endpoint_count', 0))}</strong></div>",
            f"<div class='metric'><span>Available endpoints</span><strong>{_fmt(api.get('available_endpoint_count', 0))}</strong></div>",
            f"<div class='metric'><span>Landed endpoints</span><strong>{_fmt(api.get('landed_endpoint_count', 0))}</strong></div>",
            f"<div class='metric'><span>Signal endpoints</span><strong>{_fmt(api.get('signal_endpoint_count', 0))}</strong></div>",
            f"<div class='metric'><span>High-value unused</span><strong>{_fmt(api.get('high_value_unused_count', 0))}</strong></div>",
        ]
    )
    flow = context.get("capital_flow", {})
    flow_batch = flow.get("batch", {})
    sector_flow_batch = flow.get("sector_batch", {})
    candidate_pool_snapshot = flow.get("candidate_pool", {})
    flow_focus_cards = "".join([
        f"<div class='metric'><span>全市场个股资金流覆盖</span><strong>{_fmt(flow_batch.get('fetched_rows', 0))}/{_fmt(flow_batch.get('expected_rows', 0))}</strong></div>",
        f"<div class='metric'><span>资金流覆盖率</span><strong>{_fmt(flow_batch.get('coverage_pct', 0))}%</strong></div>",
        f"<div class='metric'><span>全市场板块资金流覆盖</span><strong>{_fmt(sector_flow_batch.get('fetched_rows', 0))}/{_fmt(sector_flow_batch.get('expected_rows', 0))}</strong></div>",
        f"<div class='metric'><span>板块资金流状态</span><strong>{_fmt(sector_flow_batch.get('status') or 'missing')}</strong></div>",
        f"<div class='metric'><span>真实候选池</span><strong>{_fmt(candidate_pool_snapshot.get('stock_count', 0))}</strong></div>",
        f"<div class='metric'><span>候选池来源</span><strong>{_fmt(candidate_pool_snapshot.get('source') or '—')}</strong></div>",
    ])
    flow_rows = []
    for scope, items in (("stock_flow", flow.get("stock", [])), ("sector_flow", flow.get("sector", []))):
        for item in items:
            flow_rows.append(
                f"<tr><td>{_fmt(scope)}</td><td>{_fmt(item.get('relation'))}</td>"
                f"<td>{_fmt(item.get('latest'))}</td><td>{_fmt(item.get('rows'))}</td>"
                f"<td>{_fmt(item.get('codes'))}</td></tr>"
            )
    flow_rows = "\n".join(flow_rows) or "<tr><td colspan='5' class='muted'>暂无资金流数据</td></tr>"
    stock_flow_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('stock_code'))}</td><td>{_fmt(item.get('main_net'))}</td>"
        f"<td>{_fmt(zh_text(cn(PROVIDER_CN, item.get('provider'))))}</td><td>{_fmt(item.get('fetched_at'))}</td></tr>"
        for item in flow.get("stock_top", [])[:10]
    ) or "<tr><td colspan='4' class='muted'>暂无当前个股资金流</td></tr>"
    stock_outflow_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('stock_code'))}</td><td>{_fmt(item.get('main_net'))}</td>"
        f"<td>{_fmt(zh_text(cn(PROVIDER_CN, item.get('provider'))))}</td><td>{_fmt(item.get('fetched_at'))}</td></tr>"
        for item in flow.get("stock_bottom", [])[:10]
    ) or "<tr><td colspan='4' class='muted'>No current stock outflow</td></tr>"
    sector_flow_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('sector_name') or item.get('sector_code'))}</td><td>{_fmt(item.get('main_net'))}</td>"
        f"<td>{_fmt(zh_text(cn(PROVIDER_CN, item.get('provider'))))}</td><td>{_fmt(item.get('fetched_at'))}</td></tr>"
        for item in flow.get("sector_top", [])[:10]
    ) or "<tr><td colspan='4' class='muted'>暂无当前板块资金流</td></tr>"
    sector_outflow_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('sector_name') or item.get('sector_code'))}</td><td>{_fmt(item.get('main_net'))}</td>"
        f"<td>{_fmt(zh_text(cn(PROVIDER_CN, item.get('provider'))))}</td><td>{_fmt(item.get('fetched_at'))}</td></tr>"
        for item in flow.get("sector_bottom", [])[:10]
    ) or "<tr><td colspan='4' class='muted'>No current sector outflow</td></tr>"
    concept = context.get("concept_status", {})
    concept_cards = "\n".join(
        f"<div class='metric'><span>{escape(label)}</span><strong>{_fmt(concept.get(key, 0))}</strong></div>"
        for label, key in (
            ("THS 当期概念", "concepts"),
            ("当期成分关系", "members"),
            ("成分完整概念", "verified"),
            ("分页未完成", "partial"),
        )
    )
    outcomes = context.get("outcome_status", {})
    outcome_cards = "\n".join(
        f"<div class='metric'><span>{escape(label)}</span><strong>{_fmt(outcomes.get(key, 0))}</strong></div>"
        for label, key in (("交易计划", "plans"), ("复盘日志", "journals"), ("人工结果", "outcomes"), ("已执行", "executed"), ("待复核", "review_required"))
    )
    qlib_status = context.get("qlib_status", {})
    qlib_cards = "\n".join(
        f"<div class='metric'><span>{escape(label)}</span><strong>{_fmt(qlib_status.get(key, 0))}</strong></div>"
        for label, key in (("模型", "models"), ("预测", "predictions"), ("评估", "evaluations"), ("候选重合", "overlap"), ("信号影响", "signal_impact"))
    )
    partial_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('concept_name') or item.get('concept_code'))}</td>"
        f"<td>{_fmt(item.get('status'))}</td><td>{_fmt(item.get('pages_fetched'))}/{_fmt(item.get('pages_expected'))}</td>"
        f"<td>{_fmt(item.get('member_rows'))}</td><td>{_fmt(item.get('last_error'))}</td></tr>"
        for item in concept.get("top_partial", [])
    ) or "<tr><td colspan='5' class='muted'>THS 概念分页全部完成</td></tr>"
    api_workflow_rows = "\n".join(
        f"<tr><td>{_fmt(stage)}</td><td>{_fmt(', '.join(endpoints))}</td></tr>"
        for stage, endpoints in (api.get("workflow_evidence") or {}).items()
    ) or "<tr><td colspan='2' class='muted'>No endpoint inventory loaded</td></tr>"

    source_rows = []
    for view_name, rows in context.get("sources", {}).items():
        if not rows:
            source_rows.append(f"<tr><td>{escape(view_name)}</td><td colspan='3' class='muted'>no rows</td></tr>")
            continue
        for row in rows:
            fallback = bool(row.get("is_fallback"))
            source_rows.append(
                f"<tr><td>{escape(view_name)}</td><td>{_fmt(row.get('source_table'))}</td>"
                f"<td><span class='pill {'warn' if fallback else 'ok'}'>{'fallback' if fallback else 'real'}</span></td>"
                f"<td>{_fmt(row.get('count'))}</td></tr>"
            )

    stage_rows = []
    for stage, stats in sorted(context.get("stage_stats", {}).items()):
        hit_rate = stats.get("hit_rate")
        avg_return = stats.get("avg_forward_return_pct")
        stage_rows.append(
            f"<tr><td>{escape(stage)}</td><td>{_fmt(stats.get('sample_count'))}</td>"
            f"<td>{_fmt(stats.get('return_sample_count'))}</td>"
            f"<td>{'' if hit_rate is None else _fmt(hit_rate) + '%'}</td>"
            f"<td>{'' if avg_return is None else _fmt(avg_return) + '%'}</td></tr>"
        )

    sector_rows = "\n".join(
        f"<tr><td>{idx}</td><td>{_fmt(item.get('sector_name') or item.get('sector_code'))}</td><td>{_fmt(item.get('score'))}</td></tr>"
        for idx, item in enumerate(context.get("sectors", [])[:10], start=1)
    )
    candidate_rows = "\n".join(
        f"<tr><td>{idx}</td><td>{_fmt(item.get('stock_name') or item.get('stock_code'))}</td><td>{_fmt(item.get('stock_code'))}</td><td>{_fmt(item.get('score'))}</td></tr>"
        for idx, item in enumerate(context.get("candidates", [])[:20], start=1)
    )
    operator_origin_cards = "\n".join(
        f"<div class='metric'><span>{_fmt(item.get('data_origin'))}</span><strong>{_fmt(item.get('count'))}</strong></div>"
        for item in context.get("operator_origin_stats", [])
    )
    operator_candidate_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('trade_date'))}</td><td>{_fmt(item.get('stage'))}</td>"
        f"<td>{_fmt(item.get('stock_name') or item.get('stock_code'))}</td><td>{_fmt(item.get('stock_code'))}</td>"
        f"<td>{_fmt(item.get('score'))}</td><td>{_fmt(item.get('decision'))}</td><td>{_fmt(item.get('data_origin'))}</td></tr>"
        for item in context.get("operator_candidates", [])[:30]
    )
    alert_rows = "\n".join(
        f"<tr><td>{_fmt(zh_text(cn(SEVERITY_CN, item.get('severity'))))}</td>"
        f"<td>{_fmt(zh_text(cn(CATEGORY_CN, item.get('category'))))}</td>"
        f"<td>{_fmt(zh_text(item.get('message')))}</td></tr>"
        for item in context.get("alerts", [])
    ) or "<tr><td colspan='3' class='empty'>今日无风险告警（按数据最新交易日过滤）</td></tr>"
    report_links = "\n".join(
        f"<a class='report-link' href='{escape(str(item.get('path')))}'>{_fmt(item.get('name'))}<span>{_fmt(item.get('size'))} bytes</span></a>"
        for item in context.get("reports", [])
    )
    gap_items = "\n".join(f"<li>{_fmt(item)}</li>" for item in context.get("gaps", []))
    strategy_candidates = context.get("strategy_candidates", [])

    page_trade_date = str(context.get("trade_date") or "")[:10]

    def stage_candidate_rows(stage_names: set[str], limit: int = 8) -> str:
        rows = [
            item
            for item in strategy_candidates
            if str(item.get("stage") or "") in stage_names
            and (
                not page_trade_date
                or str(item.get("trade_date") or "")[:10] == page_trade_date
            )
        ][:limit]
        if not rows:
            return "<tr><td colspan='6' class='muted'>暂无候选或样本不足</td></tr>"
        return "\n".join(
            f"<tr><td>{_fmt(item.get('symbol'))} {_fmt(item.get('stock_name'))}</td>"
            f"<td>{_fmt(item.get('score'))}</td><td>{_fmt(item.get('selected_reason'))}</td>"
            f"<td>{_fmt(item.get('risk_points'))}</td><td>{_fmt(item.get('invalid_conditions'))}</td>"
            f"<td>{_fmt(item.get('trade_date'))}</td></tr>"
            for item in rows
        )

    workflow_research_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('context_type'))}</td><td>{_fmt(item.get('sector') or item.get('symbol'))}</td>"
        f"<td>{_fmt(item.get('title'))}</td></tr>"
        for item in context.get("research_context", [])[:10]
    ) or "<tr><td colspan='3' class='muted'>暂无研究上下文</td></tr>"
    workflow_backtest_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('strategy_id'))}</td><td>{_fmt(item.get('stage'))}</td>"
        f"<td>{_fmt(item.get('sample_count'))}</td><td>{_fmt(item.get('win_rate'))}</td>"
        f"<td>{_fmt(item.get('avg_return'))}</td></tr>"
        for item in context.get("strategy_backtest", [])[:10]
    ) or "<tr><td colspan='5' class='muted'>暂无策略回测样本</td></tr>"
    qlib_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('model_id'))}</td><td>{_fmt(item.get('sample_count'))}</td>"
        f"<td>{_fmt(item.get('hit_rate'))}</td><td>disabled</td></tr>"
        for item in context.get("qlib_shadow", [])[:10]
    ) or "<tr><td colspan='4' class='muted'>qlib shadow 暂无预测样本，signal impact disabled</td></tr>"
    auction_evidence_rows = "\n".join(
        f"<tr><td>{_fmt(item.get('trade_date'))}</td><td>{_fmt(item.get('stock_code') or 'MARKET')}</td>"
        f"<td>{_fmt(item.get('source_table'))}</td><td>{_fmt(item.get('confirmation'))}</td>"
        f"<td>{_fmt(item.get('auction_strength'))}</td><td>{_bool_pill(item.get('is_fallback'))}</td>"
        f"<td>{_fmt(item.get('missing_reason'))}</td></tr>"
        for item in context.get("auction_evidence", [])[:12]
    ) or "<tr><td colspan='7' class='muted'>暂无竞价证据；检查 auction_tick / auction_bidding_anomaly / advanced_morning_bidding_summary</td></tr>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>职业交易检查页</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --line: #d9e0ea;
      --text: #142033;
      --muted: #627188;
      --ok: #0f766e;
      --warn: #a16207;
      --bad: #b91c1c;
      --ink: #1f3a5f;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--text); }}
    header {{ padding: 24px 32px 16px; background: #eef3f8; border-bottom: 1px solid var(--line); }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; letter-spacing: 0; }}
    main {{ width: min(1280px, calc(100vw - 32px)); margin: 20px auto 40px; display: grid; gap: 16px; }}
    section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .metric {{ border: 1px solid var(--line); border-radius: 6px; padding: 12px; min-height: 72px; background: #fbfcfe; }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 24px; color: var(--ink); }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .compact-top {{ margin-top: 16px; }}
    .compact-top h3 {{ margin: 0 0 8px; font-size: 15px; }}
    .filter-input {{ width: 100%; max-width: 360px; margin: 0 0 12px; padding: 9px 11px; border: 1px solid var(--line); border-radius: 6px; font: inherit; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; background: #f7f9fc; }}
    .pill {{ display: inline-block; min-width: 64px; padding: 3px 8px; border-radius: 999px; font-size: 12px; text-align: center; }}
    .ok {{ color: var(--ok); background: #dff7f2; }}
    .warn {{ color: var(--warn); background: #fff4ce; }}
    .bad {{ color: var(--bad); background: #fee2e2; }}
    .alert {{ margin: 0 0 14px; padding: 12px 14px; border: 1px solid #f4b4b4; border-radius: 6px; }}
    .muted {{ color: var(--muted); }}
    .reports {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
    .report-link {{ display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; text-decoration: none; color: var(--text); background: #fbfcfe; }}
    .report-link span {{ color: var(--muted); white-space: nowrap; }}
    .workflow {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .workflow a {{ display: block; border: 1px solid var(--line); border-radius: 6px; padding: 10px; text-decoration: none; color: var(--text); background: #fbfcfe; text-align: center; font-weight: 600; }}
    .workflow-stage {{ margin-top: 18px; }}
    .workflow-stage h3 {{ margin: 0 0 10px; font-size: 16px; letter-spacing: 0; }}
    ul {{ margin: 0; padding-left: 20px; }}
    @media (max-width: 860px) {{
      header {{ padding: 20px 16px 12px; }}
      main {{ width: calc(100vw - 20px); }}
      .summary, .grid-2 {{ grid-template-columns: 1fr; }}
      .workflow {{ grid-template-columns: 1fr; }}
      table {{ font-size: 13px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>职业交易检查页</h1>
    <div class="muted">交易日：{_fmt(context.get('trade_date'))} · 生成时间：{_fmt(context.get('generated_at'))}</div>
  </header>
  <main>
    {date_warning}
    <section>
      <h2>市场状态</h2>
      <div class="summary">
        <div class="metric"><span>情绪阶段</span><strong>{_fmt(market.get('regime'))}</strong></div>
        <div class="metric"><span>建议仓位上限</span><strong>{_fmt(market.get('suggested_position_pct'))}%</strong></div>
        <div class="metric"><span>情绪分</span><strong>{_fmt(market.get('regime_score'))}</strong></div>
        <div class="metric"><span>告警数量</span><strong>{len(context.get('alerts', []))}</strong></div>
        <div class="metric {exec_class}"><span>入场执行就绪</span><strong>{exec_label}</strong></div>
        <div class="metric"><span>可执行候选</span><strong>{_fmt(exec_executable)}</strong></div>
        <div class="metric"><span>分析可用候选</span><strong>{_fmt(exec_actionable)}</strong></div>
      </div>
      {f'<p class="muted">入场阻断原因：{escape(exec_block_note)}</p>' if exec_block_note and not exec_ready else ''}
      <p class="muted">说明：入场执行就绪看 is_executable；分析可用候选含 close keep/reduce。盘中 delayed 资金流仅供分析；close 的 signal_close 是复盘价，不等于可入场。</p>
      <div class="summary">
        <div class="metric {readiness_class}"><span>Certified gate</span><strong>{readiness_label}</strong></div>
        <div class="metric"><span>Source / pipeline</span><strong>{str(source_ready).lower()} / {str(pipeline_ready).lower()}</strong></div>
        <div class="metric"><span>Missing groups</span><strong>{escape(readiness_missing)}</strong></div>
      </div>
    </section>
    <section>
      <h2>操盘工作流</h2>
      <nav class="workflow">
        <a href="#pre-market">盘前</a>
        <a href="#auction">竞价</a>
        <a href="#intraday">盘中</a>
        <a href="#closing">尾盘</a>
        <a href="#review">盘后</a>
      </nav>
      <div id="pre-market" class="workflow-stage">
        <h3>盘前：主线与候选池</h3>
        <table><thead><tr><th>候选</th><th>分数</th><th>入选原因</th><th>风险点</th><th>失效条件</th><th>日期</th></tr></thead><tbody>{stage_candidate_rows({'pre_market', 'premarket_pool'})}</tbody></table>
      </div>
      <div id="auction" class="workflow-stage">
        <h3>竞价：确认与剔除</h3>
        <table><thead><tr><th>候选</th><th>分数</th><th>入选原因</th><th>风险点</th><th>失效条件</th><th>日期</th></tr></thead><tbody>{stage_candidate_rows({'auction_confirm', 'auction_confirmation'})}</tbody></table>
        <table><thead><tr><th>日期</th><th>对象</th><th>证据源</th><th>确认</th><th>强度</th><th>Fallback</th><th>缺口</th></tr></thead><tbody>{auction_evidence_rows}</tbody></table>
      </div>
      <div id="intraday" class="workflow-stage">
        <h3>盘中：强弱与告警</h3>
        <table><thead><tr><th>候选</th><th>分数</th><th>入选原因</th><th>风险点</th><th>失效条件</th><th>日期</th></tr></thead><tbody>{stage_candidate_rows({'intraday_strength'})}</tbody></table>
      </div>
      <div id="closing" class="workflow-stage">
        <h3>尾盘：去留与次日预期</h3>
        <table><thead><tr><th>候选</th><th>分数</th><th>入选原因</th><th>风险点</th><th>失效条件</th><th>日期</th></tr></thead><tbody>{stage_candidate_rows({'closing_decision', 'close_decision'})}</tbody></table>
      </div>
      <div id="review" class="workflow-stage">
        <h3>盘后：复盘、研究与旁路验证</h3>
        <div class="grid-2">
          <div>
            <table><thead><tr><th>类型</th><th>对象</th><th>标题</th></tr></thead><tbody>{workflow_research_rows}</tbody></table>
          </div>
          <div>
            <table><thead><tr><th>策略</th><th>阶段</th><th>样本</th><th>胜率</th><th>平均收益</th></tr></thead><tbody>{workflow_backtest_rows}</tbody></table>
          </div>
        </div>
        <table><thead><tr><th>qlib shadow</th><th>样本</th><th>命中率</th><th>信号影响</th></tr></thead><tbody>{qlib_rows}</tbody></table>
      </div>
    </section>
    <section>
      <h2>核心数据量</h2>
      <div class="summary">{count_cards}</div>
    </section>
    <section id="live-status" data-screen-label="live-status">
      <h2>盘中数据状态</h2>
      <input id="dashboard-filter" class="filter-input" type="search" placeholder="筛选股票、板块、来源" aria-label="筛选股票、板块、来源" />
      <div class="summary">{''.join(
        f"<div class='metric'><span>{_fmt(item.get('relation'))}</span><strong>{_fmt(item.get('latest'))}</strong></div>"
        for item in (flow.get('stock', []) + flow.get('sector', []))
      ) or "<div class='metric'><span>资金流</span><strong>暂无</strong></div>"}</div>
      <table><thead><tr><th>范围</th><th>关系</th><th>最新日期</th><th>行数</th><th>代码数</th></tr></thead><tbody>{flow_rows}</tbody></table>
      <div class="summary compact-top">{flow_focus_cards}</div>
      <div class="grid-2 compact-top">
        <div><h3>个股资金流 Top 10</h3><table id="stock-flow-table"><thead><tr><th>代码</th><th>主力净额</th><th>来源</th><th>抓取时间</th></tr></thead><tbody>{stock_flow_rows}</tbody></table></div>
        <div><h3>个股资金流出 Top 10</h3><table id="stock-outflow-table"><thead><tr><th>代码</th><th>主力净额</th><th>来源</th><th>抓取时间</th></tr></thead><tbody>{stock_outflow_rows}</tbody></table></div>
      </div>
      <div class="grid-2 compact-top">
        <div><h3>板块资金流 Top 10</h3><table id="sector-flow-table"><thead><tr><th>板块</th><th>主力净额</th><th>来源</th><th>抓取时间</th></tr></thead><tbody>{sector_flow_rows}</tbody></table></div>
        <div><h3>板块资金流出 Top 10</h3><table id="sector-outflow-table"><thead><tr><th>板块</th><th>主力净额</th><th>来源</th><th>抓取时间</th></tr></thead><tbody>{sector_outflow_rows}</tbody></table></div>
      </div>
    </section>
    <section id="concept-status" data-screen-label="concept-status">
      <h2>THS 概念完整性</h2>
      <div class="summary">{concept_cards}</div>
      <p class="muted">快照日期：{_fmt(concept.get('latest'))}；状态：{_fmt(concept.get('snapshot_status'))}；历史累计数据不计入当期口径，历史回测必须过滤 date_verified=false。</p>
      <table><thead><tr><th>概念</th><th>状态</th><th>分页</th><th>成分数</th><th>错误</th></tr></thead><tbody>{partial_rows}</tbody></table>
    </section>
    <section id="review-status" data-screen-label="review-status">
      <h2>人工复盘闭环</h2>
      <div class="summary">{outcome_cards}</div>
      <p class="muted">模板：<a href="operator_outcomes_template_20260714.csv">operator_outcomes_template_20260714.csv</a>；填写后执行 <code>D:/anaconda/python.exe scripts/import_operator_trade_outcomes.py --db kpl_data.duckdb --csv &lt;file&gt;</code>。当前最后结果日：{_fmt(outcomes.get('latest_date') or '暂无')}。</p>
    </section>
    <section id="qlib-status" data-screen-label="qlib-status">
      <h2>QLib 影子实验</h2>
      <div class="summary">{qlib_cards}</div>
      <p class="muted">QLib 当前只做旁路评估，signal impact disabled，不参与主信号和仓位建议。</p>
    </section>
    <section>
      <h2>API Utilization</h2>
      <div class="summary">{api_cards}</div>
      <table><thead><tr><th>Workflow</th><th>API Evidence</th></tr></thead><tbody>{api_workflow_rows}</tbody></table>
    </section>
    <section>
      <h2>Unified Operator Candidates</h2>
      <div class="summary">{operator_origin_cards}</div>
      <table><thead><tr><th>Date</th><th>Stage</th><th>Name</th><th>Code</th><th>Score</th><th>Decision</th><th>Origin</th></tr></thead><tbody>{operator_candidate_rows}</tbody></table>
    </section>
    <section class="grid-2">
      <div>
        <h2>数据链路</h2>
        <table><thead><tr><th>链路</th><th>状态</th><th>可用来源</th><th>缺失必需项</th></tr></thead><tbody>{''.join(chain_rows)}</tbody></table>
      </div>
      <div>
        <h2>标准化来源</h2>
        <table><thead><tr><th>视图</th><th>来源</th><th>类型</th><th>行数</th></tr></thead><tbody>{''.join(source_rows)}</tbody></table>
      </div>
    </section>
    <section>
      <h2>四阶段候选股回测</h2>
      <table><thead><tr><th>阶段</th><th>信号数</th><th>收益样本</th><th>命中率</th><th>平均收益</th></tr></thead><tbody>{''.join(stage_rows)}</tbody></table>
    </section>
    <section class="grid-2">
      <div>
        <h2>板块主线</h2>
        <table><thead><tr><th>Rank</th><th>板块</th><th>分数</th></tr></thead><tbody>{sector_rows}</tbody></table>
      </div>
      <div>
        <h2>候选股</h2>
        <table><thead><tr><th>Rank</th><th>名称</th><th>代码</th><th>分数</th></tr></thead><tbody>{candidate_rows}</tbody></table>
      </div>
    </section>
    <section>
      <h2>风险告警</h2>
      <table><thead><tr><th>级别</th><th>类别</th><th>信息</th></tr></thead><tbody>{alert_rows}</tbody></table>
    </section>
    <section>
      <h2>当前缺口</h2>
      <ul>{gap_items}</ul>
    </section>
    <section>
      <h2>Markdown 报告入口</h2>
      <div class="reports">{report_links}</div>
    </section>
  </main>
  <script>
    (() => {{
      const input = document.getElementById('dashboard-filter');
      if (!input) return;
      const tables = [document.getElementById('stock-flow-table'), document.getElementById('sector-flow-table')].filter(Boolean);
      input.addEventListener('input', () => {{
        const needle = input.value.trim().toLowerCase();
        tables.forEach((table) => {{
          table.querySelectorAll('tbody tr').forEach((row) => {{
            row.hidden = Boolean(needle) && !row.textContent.toLowerCase().includes(needle);
          }});
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def write_dashboard(
    db_path: str | Path,
    out_path: str | Path = "reports/trading_dashboard_latest.html",
    trade_date: str | None = None,
) -> Path:
    path = Path(out_path)
    context = load_dashboard_context(db_path, path.parent, trade_date=trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dashboard_html(context), encoding="utf-8")
    return path
