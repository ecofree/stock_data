"""Daily manual operator workflow loop.

This module turns generated signals into auditable manual workflow records.
It does not create orders and does not imply automatic execution.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from trade_system.operator_risk import TradePlanInput, evaluate_trade_plan
from trade_system.quality import table_columns, table_exists
from trade_system.i18n_labels import zh_text
from trade_system.readiness import assess_trade_date_readiness
from trade_system.risk import init_trading_tables
from trade_system.db_utils import fetch_dicts as _fetch_dicts



def _loads(value: Any) -> dict:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _latest_regime(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict:
    rows = _fetch_dicts(
        con,
        """
        SELECT trade_date, regime, regime_score, suggested_position_pct, evidence_json
        FROM market_regime_snapshot
        WHERE trade_date = ?
        ORDER BY generated_at DESC NULLS LAST
        LIMIT 1
        """,
        [trade_date],
    )
    if not rows:
        return {
            "trade_date": trade_date,
            "regime": "unknown",
            "regime_score": 0,
            "suggested_position_pct": 0,
            "evidence": {},
        }
    row = rows[0]
    row["evidence"] = _loads(row.get("evidence_json"))
    return row


def _risk_state(regime: dict) -> str:
    suggested = int(regime.get("suggested_position_pct") or 0)
    acute = float((regime.get("evidence") or {}).get("acute_drop_risk_score") or 0)
    if suggested <= 15 or acute >= 60:
        return "defensive"
    if suggested <= 30 or acute >= 45:
        return "cautious"
    return "normal"


def _max_single_position(regime: dict) -> float:
    state = _risk_state(regime)
    suggested = float(regime.get("suggested_position_pct") or 0)
    if state == "defensive":
        return min(5.0, max(0.0, suggested))
    if state == "cautious":
        return min(8.0, max(3.0, suggested / 3.0))
    return min(10.0, max(5.0, suggested / 4.0))


def _candidate_rows(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    limit: int,
    signal_stage: str | None = None,
) -> list[dict]:
    score_columns = table_columns(con, "stock_candidate_score")
    actionable_filter = ""
    if "is_actionable" in score_columns:
        actionable_filter += "\n          AND coalesce(stock_candidate_score.is_actionable, false) = true"
    stage_columns = table_columns(con, "stock_candidate_stage_signal") if table_exists(con, "stock_candidate_stage_signal") else set()
    if signal_stage and "is_actionable" in stage_columns:
        execution_filters = ["coalesce(s.is_actionable,false)=true"]
        if signal_stage in {"auction_confirmation", "intraday_strength"}:
            if "data_complete" in stage_columns:
                execution_filters.append("coalesce(s.data_complete,false)=true")
            if "signal_triggered" in stage_columns:
                execution_filters.append("coalesce(s.signal_triggered,false)=true")
            if "tradable" in stage_columns:
                execution_filters.append("coalesce(s.tradable,false)=true")
        sector_expr = (
            """
                (
                    SELECT c.sector_code
                    FROM stock_candidate_score c
                    WHERE c.trade_date=s.trade_date
                      AND c.stock_code=s.stock_code
                    LIMIT 1
                )
            """
            if "sector_code" in score_columns
            else "CAST(NULL AS VARCHAR)"
        )
        return _fetch_dicts(
            con,
            f"""
            SELECT
                s.trade_date,
                s.stock_code,
                s.stock_name,
                s.score,
                'stage_signal' AS source,
                {sector_expr} AS sector_code,
                s.evidence_json
            FROM stock_candidate_stage_signal s
            WHERE CAST(s.trade_date AS VARCHAR)=?
              AND s.stage=?
              AND {" AND ".join(execution_filters)}
            ORDER BY s.score DESC NULLS LAST, s.stock_code
            LIMIT ?
            """,
            [trade_date, signal_stage, int(limit)],
        )
    stage_gate = "is_actionable"
    stage_params: list[Any] = []
    stage_filter = ""
    if table_exists(con, "stock_candidate_stage_signal") and stage_gate in stage_columns:
        if signal_stage and "stage" in stage_columns:
            stage_filter = "AND s.stage = ?"
            stage_params.append(signal_stage)
        actionable_filter = f"""
          AND EXISTS (
              SELECT 1
              FROM stock_candidate_stage_signal s
              WHERE s.trade_date = stock_candidate_score.trade_date
                AND s.stock_code = stock_candidate_score.stock_code
                {stage_filter}
                AND coalesce(s.{stage_gate}, false) = true
          )
        """
    rows = _fetch_dicts(
        con,
        f"""
        SELECT trade_date, stock_code, stock_name, score, source, sector_code, evidence_json
        FROM stock_candidate_score
        WHERE trade_date = ?
        {actionable_filter}
        ORDER BY score DESC NULLS LAST, stock_code
        LIMIT {int(limit)}
        """,
        [trade_date, *stage_params],
    )
    if rows or not table_exists(con, "stock_candidate_stage_signal"):
        return rows
    # Intraday runs can have a fully evidence-gated stage pool before the
    # legacy close-stage score table is produced.  Promote those rows into the
    # manual workflow without inventing a score or an order: the existing
    # risk/regime gate still decides whether a plan is blocked or reviewable.
    stage_actionable_filter = (
        f"AND coalesce({stage_gate}, false) = true"
        if stage_gate in stage_columns
        else ""
    )
    fallback_stage_filter = ""
    fallback_params: list[Any] = [trade_date]
    if signal_stage and "stage" in stage_columns:
        fallback_stage_filter = "AND stage = ?"
        fallback_params.append(signal_stage)
    fallback_params.append(int(limit))
    stage_rows = _fetch_dicts(
        con,
        f"""
        WITH ranked AS (
            SELECT trade_date, stock_code, stock_name, score, evidence_json,
                   row_number() OVER (
                       PARTITION BY stock_code
                       ORDER BY CASE stage
                           WHEN 'close_decision' THEN 1
                           WHEN 'intraday_strength' THEN 2
                           WHEN 'auction_confirmation' THEN 3
                           ELSE 4 END,
                           score DESC NULLS LAST
                   ) AS rn
            FROM stock_candidate_stage_signal
            WHERE CAST(trade_date AS VARCHAR) = ?
              {fallback_stage_filter}
              {stage_actionable_filter}
        )
        SELECT trade_date, stock_code, stock_name, score,
               'stage_signal' AS source, NULL AS sector_code, evidence_json
        FROM ranked
        WHERE rn = 1
        ORDER BY score DESC NULLS LAST, stock_code
        LIMIT ?
        """,
        fallback_params,
    )
    return stage_rows


def _stage_rows(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    stock_codes: list[str],
    signal_stage: str | None = None,
) -> list[dict]:
    if not stock_codes:
        return []
    placeholders = ", ".join(["?"] * len(stock_codes))
    stage_columns = table_columns(con, "stock_candidate_stage_signal")
    stage_gate = "is_executable" if "is_executable" in stage_columns else "is_actionable"
    actionable_filter = (
        f"AND coalesce({stage_gate}, false) = true"
        if stage_gate in stage_columns
        else ""
    )
    stage_filter = ""
    stage_params: list[Any] = []
    if signal_stage and "stage" in stage_columns:
        stage_filter = "AND stage = ?"
        stage_params.append(signal_stage)
    return _fetch_dicts(
        con,
        f"""
        SELECT trade_date, stage, stock_code, stock_name, score, decision, evidence_json
        FROM stock_candidate_stage_signal
        WHERE trade_date = ? AND stock_code IN ({placeholders})
          {stage_filter}
          {actionable_filter}
        ORDER BY stock_code, stage
        """,
        [trade_date] + stock_codes + stage_params,
    )


def run_daily_operator_loop(
    db_path: str | Path,
    trade_date: str,
    limit: int = 20,
    stage: str = "close",
) -> dict[str, int]:
    stage = str(stage or "close").lower()
    if stage not in {"auction", "intraday", "close"}:
        raise ValueError(f"unsupported operator stage: {stage}")
    signal_stage = {
        "auction": "auction_confirmation",
        "intraday": "intraday_strength",
        "close": "close_decision",
    }[stage]
    init_trading_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        # The indexed workflow tables are refreshed idempotently below.  Do
        # not DELETE and reinsert the same unique keys in one DuckDB
        # transaction: DuckDB can retain the deleted key in the unique-index
        # delta until commit and reject the replacement.  Removing stale
        # candidates after the current pool is known avoids both that failure
        # mode and duplicate rows on retries.
        con.execute(
            "DELETE FROM trade_journal WHERE trade_date = ? AND action = ? "
            "AND mistake_tag = 'pending_review' AND starts_with(reason, '[daily_loop:v2] ')",
            [trade_date, signal_stage],
        )

        regime = _latest_regime(con, trade_date)
        risk_state = _risk_state(regime)
        suggested = float(regime.get("suggested_position_pct") or 0)
        stage_columns_for_gate = (
            table_columns(con, "stock_candidate_stage_signal")
            if table_exists(con, "stock_candidate_stage_signal")
            else set()
        )
        has_operational_checkpoint = (
            table_exists(con, "intraday_stock_flow_batch")
            or table_exists(con, "intraday_sector_flow_batch")
            or "is_executable" in stage_columns_for_gate
        )
        data_readiness = (
            assess_trade_date_readiness(
                con,
                trade_date,
                stage,
                max_age_seconds={
                    "auction": 300,
                    "intraday": 600,
                    "close": 7200,
                }[stage],
            )
            if has_operational_checkpoint
            else {"ready": True, "status": "legacy_test_or_manual_context"}
        )
        data_blocked = (
            suggested <= 0
            or str(regime.get("regime") or "") in {"数据缺失", "unknown"}
            or not data_readiness.get("source_ready", data_readiness.get("ready", False))
        )
        max_single = _max_single_position(regime)
        max_sector = min(max(0.0, suggested), 20.0)
        risk_evidence = {
            "regime": regime.get("regime"),
            "regime_score": regime.get("regime_score"),
            "suggested_position_pct": int(regime.get("suggested_position_pct") or 0),
            "acute_drop_risk_score": (regime.get("evidence") or {}).get("acute_drop_risk_score"),
            "data_readiness": data_readiness,
            "account_state": "unverified",
            "execution_blockers": ["account_snapshot_unverified"],
            "scope": "research_only",
            "note": "manual planning guardrail; no automatic order execution",
        }
        con.execute(
            """
                INSERT OR REPLACE INTO risk_snapshot (
                trade_date, total_position_pct, max_single_position_pct, max_sector_position_pct,
                daily_loss_limit_pct, current_drawdown_pct, risk_state, evidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [trade_date, None, max_single, max_sector, 2.0 if risk_state == "defensive" else 3.0, None, risk_state, json.dumps(risk_evidence, ensure_ascii=False)],
        )

        if (
            stage in {"auction", "intraday"}
            and {"risk_approved", "is_executable"}.issubset(
                stage_columns_for_gate
            )
        ):
            # Every risk pass is a complete recalculation for the active
            # stage.  Clear approvals first so a candidate that no longer
            # triggers cannot retain an executable flag from an earlier run.
            con.execute(
                "UPDATE stock_candidate_stage_signal "
                "SET risk_approved=false,is_executable=false "
                "WHERE CAST(trade_date AS VARCHAR)=? AND stage=?",
                [trade_date, signal_stage],
            )

        candidates = _candidate_rows(
            con, trade_date, limit, signal_stage=signal_stage
        )
        candidate_codes = [
            str(row.get("stock_code") or "")
            for row in candidates
            if row.get("stock_code")
        ]
        for table in ("watchlist", "trade_plan"):
            if candidate_codes:
                placeholders = ", ".join(["?"] * len(candidate_codes))
                con.execute(
                    f"DELETE FROM {table} WHERE trade_date = ? "
                    f"AND stock_code NOT IN ({placeholders})",
                    [trade_date] + candidate_codes,
                )
            else:
                con.execute(f"DELETE FROM {table} WHERE trade_date = ?", [trade_date])
        # Portfolio rows are account facts, not generated planning output.
        # The legacy table cannot prove account completeness, freshness or
        # reserved cash. Preserve it and fail closed until a verified account
        # snapshot contract is wired in; absent evidence does not mean flat.
        watchlist_count = 0
        trade_plan_count = 0
        for priority, row in enumerate(candidates, start=1):
            evidence = _loads(row.get("evidence_json"))
            risk_points = evidence.get("risk_points") or []
            thesis = zh_text(evidence.get("entry_reason")
                             or f"候选得分={float(row.get('score') or 0):.2f}")
            invalidation = zh_text(
                evidence.get("invalidation")
                or "Invalidate if score/fallback/risk evidence deteriorates."
            )
            planned_position = 0.0
            risk_flags_for_plan = list(risk_points if risk_state == "defensive" and float(row.get("score") or 0) < 70 else ())
            risk_flags_for_plan.append("account_snapshot_unverified")
            con.execute(
                """
                INSERT OR REPLACE INTO watchlist (
                    trade_date, stock_code, stock_name, sector_code, thesis, invalidation, priority, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    trade_date,
                    row.get("stock_code"),
                    row.get("stock_name"),
                    row.get("sector_code"),
                    thesis,
                    invalidation,
                    priority,
                    "blocked_data_quality" if data_blocked else "active",  # status 值渲染时经 i18n 映射
                ],
            )
            watchlist_count += 1

            decision = evaluate_trade_plan(
                TradePlanInput(
                    stock_code=row.get("stock_code") or "",
                    score=float(row.get("score") or 0),
                    market_regime=str(regime.get("regime") or "unknown"),
                    planned_position_pct=planned_position,
                    current_total_position_pct=None,
                    risk_flags=tuple(risk_flags_for_plan),
                )
            )
            con.execute(
                """
                INSERT OR REPLACE INTO trade_plan (
                    trade_date, stock_code, stock_name, setup_type, entry_condition, stop_condition,
                    target_condition, max_position_pct, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    trade_date,
                    row.get("stock_code"),
                    row.get("stock_name"),
                    "manual_shortline_plan",
                    zh_text(
                        f"Only consider after auction/intraday evidence confirms; "
                        f"risk_gate={decision.reason}; account_snapshot_unverified"
                    ),
                    zh_text(invalidation),
                    zh_text("Review at close; no automatic execution."),
                     planned_position,
                    (
                        "blocked_data_quality"
                        if data_blocked
                        else "planned" if decision.allowed else "review_required"
                    ),
                ],
            )
            if (
                stage in {"auction", "intraday"}
                and table_exists(con, "stock_candidate_stage_signal")
            ):
                stage_columns = table_columns(con, "stock_candidate_stage_signal")
                if {"risk_approved", "is_executable"}.issubset(stage_columns):
                    valid_col = ", execution_valid_until" if "execution_valid_until" in stage_columns else ""
                    current = con.execute(
                        f"SELECT evidence_json, readiness_json{valid_col} "
                        "FROM stock_candidate_stage_signal "
                        "WHERE trade_date=? AND stock_code=? AND stage=?",
                        [trade_date, row.get("stock_code"), signal_stage],
                    ).fetchone()
                    current_evidence = _loads(current[0] if current else None)
                    current_readiness = _loads(current[1] if current else None)
                    valid_until = current[2] if current and valid_col else None
                    valid_now = valid_until is None or valid_until >= datetime.now()
                    executable = bool(
                        decision.allowed
                        and row.get("stock_code")
                        and valid_now
                        and signal_stage in {"auction_confirmation", "intraday_strength"}
                    )
                    current_evidence.update({
                        "risk_approved": bool(decision.allowed),
                        "entry_executable": executable,
                        "entry_block_reason": None if executable else (
                            current_evidence.get("entry_block_reason")
                            or decision.reason
                        ),
                    })
                    current_readiness.update({
                        "row_scope": "candidate",
                        "execution_ready": executable,
                        "risk_approved_candidates": int(bool(decision.allowed)),
                        "executable_candidates": int(executable),
                    })
                    con.execute(
                        f"""
                        UPDATE stock_candidate_stage_signal
                        SET risk_approved=?, is_executable=?, evidence_json=?, readiness_json=?
                        WHERE trade_date=? AND stock_code=? AND stage=?
                        """,
                        [
                            bool(decision.allowed),
                            executable,
                            json.dumps(current_evidence, ensure_ascii=False, default=str),
                            json.dumps(current_readiness, ensure_ascii=False, default=str),
                            trade_date,
                            row.get("stock_code"),
                            signal_stage,
                        ],
                    )
            trade_plan_count += 1

        stage_rows = _stage_rows(
            con,
            trade_date,
            [row.get("stock_code") for row in candidates],
            signal_stage=signal_stage,
        )
        journal_count = 0
        stage_time = {
            "premarket_pool": "pre_market",
            "auction_confirmation": "auction",
            "intraday_strength": "intraday",
            "close_decision": "close",
        }
        for row in stage_rows:
            con.execute(
                """
                INSERT INTO trade_journal (
                    trade_date, stock_code, stock_name, action, action_time, price,
                    position_pct, reason, mistake_tag
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    trade_date,
                    row.get("stock_code"),
                    row.get("stock_name"),
                    row.get("stage"),
                    stage_time.get(row.get("stage"), "review"),
                    None,
                    0.0,
                    f"[daily_loop:v2] decision={row.get('decision')} score={float(row.get('score') or 0):.2f}",
                    "pending_review",
                ],
            )
            journal_count += 1

        result = {
            "watchlist": watchlist_count,
            "trade_plan": trade_plan_count,
            "risk_snapshot": 1,
            "portfolio_snapshot": 0,
            "trade_journal": journal_count,
        }
        con.commit()
        return result
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()
