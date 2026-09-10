"""Professional operator report snapshot builder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_columns, table_exists


SIGNAL_TABLES = {
    "daily_summary",
    "market_rise_fall",
    "market_emotion_money",
    "daily_new_high",
    "sector_strength",
    "sector_capital",
    "sector_all_stocks",
    "sector_son_plates",
    "sector_sub_concepts",
    "sector_boom_reason",
    "sector_strength_batch",
    "sector_strength_ndays",
    "sector_strength_dataframe",
    "kline",
    "index_kline",
    "index_intraday",
    "index_full_info",
    "l2_realtime_index_list",
    "l2_realtime_index_trend",
    "auction_tick",
    "auction_bidding_anomaly",
    "advanced_morning_bidding_summary",
    "advanced_morning_bidding_list",
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

RESEARCH_REVIEW_TABLES = {
    "advanced_news_flash",
    "advanced_news_flash_top",
    "news_theme",
    "news_plate",
    "advanced_corporate_news",
    "advanced_interviews",
    "tuyere_by_stock",
    "tuyere_tags",
    "stock_company_info",
    "lhb_list",
    "lhb_detail",
    "lhb_raw_list",
    "lhb_youzi_dongxiang",
    "advanced_agency_list",
    "advanced_business_list",
    "advanced_on_the_lhb",
}


def _fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _count(con: duckdb.DuckDBPyConnection, relation: str) -> int:
    if not table_exists(con, relation):
        return 0
    return int(con.execute(f'SELECT count(*) FROM "{relation}"').fetchone()[0])


def _api_endpoint_rows(con: duckdb.DuckDBPyConnection) -> list[dict]:
    if not table_exists(con, "api_endpoint_inventory"):
        return []
    return _fetch_dicts(
        con,
        """
        SELECT endpoint, table_name, table_rows, verdict, usefulness
        FROM api_endpoint_inventory
        ORDER BY endpoint
        """,
    )


def _workflow_evidence(rows: list[dict]) -> dict[str, list[str]]:
    def endpoints_for(table_names: set[str]) -> list[str]:
        endpoints = [
            row["endpoint"]
            for row in rows
            if row.get("table_name") in table_names and int(row.get("table_rows") or 0) > 0
        ]
        return endpoints[:20]

    return {
        "pre_market": endpoints_for(
            {
                "daily_summary",
                "market_rise_fall",
                "market_emotion_money",
                "sector_strength",
                "sector_capital",
                "sector_all_stocks",
                "sector_boom_reason",
                "kline",
            }
        ),
        "auction": endpoints_for(
            {
                "auction_tick",
                "auction_bidding_anomaly",
                "advanced_morning_bidding_summary",
                "advanced_morning_bidding_list",
            }
        ),
        "intraday": endpoints_for(
            {
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
                "l2_realtime_index_trend",
                "l2_sector_volume",
            }
        ),
        "close": endpoints_for({"kline", "index_kline", "index_intraday", "l2_realtime_index_list"}),
        "post_market": endpoints_for(RESEARCH_REVIEW_TABLES),
    }


def _view_degradation(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = []
    for view_name in (
        "v_auction_status",
        "v_sector_capital",
        "v_index_state",
        "v_kline_daily",
        "v_intraday_capital_flow_evidence",
        "v_intraday_strength_evidence",
        "v_theme_mainline_evidence",
        "v_research_event_evidence",
        "v_lhb_review_evidence",
    ):
        if not table_exists(con, view_name):
            rows.append({"view": view_name, "rows": 0, "fallback_rows": 0, "status": "missing"})
            continue
        cols = table_columns(con, view_name)
        total = int(con.execute(f'SELECT count(*) FROM "{view_name}"').fetchone()[0])
        fallback_rows = 0
        if "is_fallback" in cols:
            fallback_rows = int(
                con.execute(f'SELECT count(*) FROM "{view_name}" WHERE is_fallback = true').fetchone()[0]
            )
        status = "empty" if total == 0 else ("fallback" if fallback_rows else "real")
        rows.append({"view": view_name, "rows": total, "fallback_rows": fallback_rows, "status": status})
    return rows


def _api_utilization(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = _api_endpoint_rows(con)
    available_verdicts = {"stable_available", "api_available"}
    verdict_counts: dict[str, int] = {}
    for row in rows:
        verdict = row.get("verdict") or "unknown"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    landed = [row for row in rows if int(row.get("table_rows") or 0) > 0]
    signal = [row for row in landed if row.get("table_name") in SIGNAL_TABLES]
    research_review = [row for row in landed if row.get("table_name") in RESEARCH_REVIEW_TABLES]
    high_value_unused = [
        row
        for row in rows
        if row.get("verdict") in available_verdicts
        and int(row.get("table_rows") or 0) == 0
        and str(row.get("usefulness") or "").startswith("professional")
    ]
    return {
        "discovered_endpoint_count": len(rows),
        "available_endpoint_count": sum(1 for row in rows if row.get("verdict") in available_verdicts),
        "landed_endpoint_count": len(landed),
        "signal_endpoint_count": len(signal),
        "research_review_endpoint_count": len(research_review),
        "high_value_unused_count": len(high_value_unused),
        "verdict_counts": verdict_counts,
        "workflow_evidence": _workflow_evidence(rows),
        "degradation": _view_degradation(con),
    }


def _operator_outcome_stats(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    if not table_exists(con, "operator_trade_outcome"):
        return {"outcome_count": 0, "executed_outcome_count": 0, "avg_outcome_net_return_pct": None}
    cols = set(table_columns(con, "operator_trade_outcome"))
    date_filter = "WHERE trade_date = ?" if "trade_date" in cols else ""
    params = [trade_date] if date_filter else []
    outcome_count = int(con.execute(f"SELECT count(*) FROM operator_trade_outcome {date_filter}", params).fetchone()[0])
    executed_count = 0
    if "execution_status" in cols:
        executed_count = int(
            con.execute(
                f"SELECT count(*) FROM operator_trade_outcome {date_filter} "
                + ("AND execution_status = 'executed'" if date_filter else "WHERE execution_status = 'executed'"),
                params,
            ).fetchone()[0]
        )
    avg_return = None
    if "net_return_pct" in cols:
        avg_return = con.execute(
            f"SELECT avg(net_return_pct) FROM operator_trade_outcome {date_filter}",
            params,
        ).fetchone()[0]
    return {
        "outcome_count": outcome_count,
        "executed_outcome_count": executed_count,
        "avg_outcome_net_return_pct": round(float(avg_return), 2) if avg_return is not None else None,
    }


def build_operator_report_snapshot(db_path: str | Path, trade_date: str) -> dict[str, Any]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if table_exists(con, "data_source_catalog"):
            source_count, enabled_count = con.execute(
                "SELECT count(*), sum(CASE WHEN enabled THEN 1 ELSE 0 END) FROM data_source_catalog"
            ).fetchone()
        else:
            source_count, enabled_count = 0, 0

        top_candidates = []
        if table_exists(con, "strategy_scan_result"):
            top_candidates = _fetch_dicts(
                con,
                """
                SELECT symbol, stock_name, stage, score, selected_reason, risk_points, invalid_conditions
                FROM strategy_scan_result
                ORDER BY score DESC, symbol
                LIMIT 20
                """,
            )

        if table_exists(con, "strategy_backtest_result"):
            strategy_backtest = con.execute(
                """
                SELECT coalesce(sum(sample_count), 0), avg(win_rate)
                FROM strategy_backtest_result
                """
            ).fetchone()
        else:
            strategy_backtest = (0, None)

        if table_exists(con, "qlib_shadow_evaluation"):
            qlib_shadow = con.execute(
                "SELECT coalesce(sum(sample_count), 0), count(DISTINCT model_id) FROM qlib_shadow_evaluation"
            ).fetchone()
        else:
            qlib_shadow = (0, 0)

        return {
            "trade_date": trade_date,
            "data_layer": {
                "source_count": int(source_count or 0),
                "enabled_count": int(enabled_count or 0),
            },
            "candidate_layer": {
                "candidate_count": _count(con, "strategy_scan_result"),
                "top_candidates": top_candidates,
            },
            "research_layer": {
                "news_count": _count(con, "news_radar_item"),
                "note_count": _count(con, "research_note"),
                "report_count": _count(con, "research_report_file"),
            },
            "strategy_backtest": {
                "sample_count": int(strategy_backtest[0] or 0),
                "avg_win_rate": round(float(strategy_backtest[1]), 2) if strategy_backtest[1] is not None else None,
            },
            "qlib_shadow": {
                "sample_count": int(qlib_shadow[0] or 0),
                "models": int(qlib_shadow[1] or 0),
                "signal_impact": "disabled",
            },
            "api_utilization": _api_utilization(con),
            "operator_loop": {
                "watchlist_count": _count(con, "watchlist"),
                "trade_plan_count": _count(con, "trade_plan"),
                "risk_snapshot_count": _count(con, "risk_snapshot"),
                "trade_journal_count": _count(con, "trade_journal"),
                **_operator_outcome_stats(con, trade_date),
            },
            "risk_layer": {
                "risk_note": (
                    "Weak-market downgrade remains mandatory. Candidates must keep explicit "
                    "risk points and invalidation conditions. No automatic order execution."
                ),
            },
        }
    finally:
        con.close()


def persist_operator_report_snapshot(db_path: str | Path, report_type: str, snapshot: dict[str, Any]) -> int:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_report_snapshot (
                trade_date VARCHAR,
                report_type VARCHAR,
                snapshot_json VARCHAR,
                generated_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            "DELETE FROM operator_report_snapshot WHERE trade_date = ? AND report_type = ?",
            [snapshot["trade_date"], report_type],
        )
        con.execute(
            "INSERT INTO operator_report_snapshot (trade_date, report_type, snapshot_json) VALUES (?, ?, ?)",
            [snapshot["trade_date"], report_type, json.dumps(snapshot, ensure_ascii=False, sort_keys=True)],
        )
        return 1
    finally:
        con.close()


def render_operator_report_markdown(snapshot: dict[str, Any]) -> str:
    api = snapshot.get("api_utilization", {})
    operator_loop = snapshot.get("operator_loop", {})
    workflow = api.get("workflow_evidence", {})
    degradation = api.get("degradation", [])
    lines = [
        "# Professional Operator Report",
        "",
        f"- Trade date: `{snapshot['trade_date']}`",
        f"- Data sources: `{snapshot['data_layer']['enabled_count']}/{snapshot['data_layer']['source_count']}` enabled",
        f"- Strategy candidates: `{snapshot['candidate_layer']['candidate_count']}`",
        f"- Strategy backtest samples: `{snapshot['strategy_backtest']['sample_count']}`",
        f"- qlib shadow: `{snapshot['qlib_shadow']['sample_count']}` samples, signal impact `{snapshot['qlib_shadow']['signal_impact']}`",
        "",
        "## API Utilization",
        "",
        f"- Discovered endpoints: `{api.get('discovered_endpoint_count', 0)}`",
        f"- Available endpoints: `{api.get('available_endpoint_count', 0)}`",
        f"- Landed endpoints: `{api.get('landed_endpoint_count', 0)}`",
        f"- Endpoints used by signal layer: `{api.get('signal_endpoint_count', 0)}`",
        f"- High-value available but unused endpoints: `{api.get('high_value_unused_count', 0)}`",
        f"- Verdict counts: `{api.get('verdict_counts', {})}`",
        "",
        "### Workflow Evidence",
        "",
        f"- Pre-market: `{', '.join(workflow.get('pre_market', []))}`",
        f"- Auction: `{', '.join(workflow.get('auction', []))}`",
        f"- Intraday: `{', '.join(workflow.get('intraday', []))}`",
        f"- Close: `{', '.join(workflow.get('close', []))}`",
        f"- Post-market: `{', '.join(workflow.get('post_market', []))}`",
        "",
        "### Data Degradation",
        "",
        "| View | Rows | Fallback Rows | Status |",
        "|---|---:|---:|---|",
    ]
    for row in degradation:
        lines.append(
            f"| {row.get('view', '')} | {int(row.get('rows') or 0)} | "
            f"{int(row.get('fallback_rows') or 0)} | {row.get('status', '')} |"
        )

    lines.extend(
        [
            "",
            "## Operator Loop",
            "",
            f"- Watchlist rows: `{operator_loop.get('watchlist_count', 0)}`",
            f"- Trade plan rows: `{operator_loop.get('trade_plan_count', 0)}`",
            f"- Risk snapshot rows: `{operator_loop.get('risk_snapshot_count', 0)}`",
            f"- Trade journal rows: `{operator_loop.get('trade_journal_count', 0)}`",
            "",
            "### Operator Outcomes",
            "",
            f"- Outcome rows: `{operator_loop.get('outcome_count', 0)}`",
            f"- Executed outcomes: `{operator_loop.get('executed_outcome_count', 0)}`",
            f"- Average outcome net return: `{operator_loop.get('avg_outcome_net_return_pct')}`",
            "",
            "## Candidates",
            "",
            "| Symbol | Stage | Score | Selected Reason | Risk Points | Invalid Conditions |",
            "|---|---|---:|---|---|---|",
        ]
    )
    for row in snapshot["candidate_layer"].get("top_candidates", [])[:30]:
        lines.append(
            f"| {row.get('symbol', '')} {row.get('stock_name', '')} | {row.get('stage', '')} | "
            f"{float(row.get('score') or 0):.2f} | {row.get('selected_reason', '')} | "
            f"{row.get('risk_points', '')} | {row.get('invalid_conditions', '')} |"
        )

    lines.extend(
        [
            "",
            "## Research And Review",
            "",
            f"- News: `{snapshot['research_layer'].get('news_count', 0)}`",
            f"- Research notes: `{snapshot['research_layer'].get('note_count', 0)}`",
            f"- Research reports/files: `{snapshot['research_layer'].get('report_count', 0)}`",
            "",
            "## Risk",
            "",
            f"- {snapshot['risk_layer']['risk_note']}",
            "",
        ]
    )
    return "\n".join(lines)
