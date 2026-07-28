"""As-of-time stage signal generation without same-day look-ahead leakage."""

from __future__ import annotations

from datetime import datetime, time
import json
from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_columns, table_exists
from trade_system.readiness import assess_trade_date_readiness


STAGE_NAMES = (
    "premarket_pool",
    "auction_confirmation",
    "intraday_strength",
    "close_decision",
)

FEATURE_VERSION = "stage_v2_asof"


def _fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params=None) -> list[dict]:
    cur = con.execute(sql, params or [])
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def ensure_stage_signal_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_candidate_stage_signal (
            trade_date VARCHAR,
            stage VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            score DOUBLE,
            decision VARCHAR,
            evidence_json VARCHAR,
            generated_at TIMESTAMP DEFAULT current_timestamp,
            source_trade_date VARCHAR,
            as_of_time TIMESTAMP,
            run_id VARCHAR,
            is_actionable BOOLEAN DEFAULT false,
            readiness_json VARCHAR,
            reference_price DOUBLE,
            reference_price_type VARCHAR,
        feature_version VARCHAR
            ,data_complete BOOLEAN DEFAULT false
            ,signal_triggered BOOLEAN DEFAULT false
            ,tradable BOOLEAN DEFAULT false
            ,risk_approved BOOLEAN DEFAULT false
            ,is_executable BOOLEAN DEFAULT false
        )
        """
    )
    additions = {
        "source_trade_date": "VARCHAR",
        "as_of_time": "TIMESTAMP",
        "run_id": "VARCHAR",
        "is_actionable": "BOOLEAN DEFAULT false",
        "readiness_json": "VARCHAR",
        "reference_price": "DOUBLE",
        "reference_price_type": "VARCHAR",
        "feature_version": "VARCHAR",
        "data_complete": "BOOLEAN DEFAULT false",
        "signal_triggered": "BOOLEAN DEFAULT false",
        "tradable": "BOOLEAN DEFAULT false",
        "risk_approved": "BOOLEAN DEFAULT false",
        "is_executable": "BOOLEAN DEFAULT false",
    }
    existing = set(table_columns(con, "stock_candidate_stage_signal"))
    for column, data_type in additions.items():
        if column not in existing:
            con.execute(
                f'ALTER TABLE stock_candidate_stage_signal ADD COLUMN "{column}" {data_type}'
            )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_stage_signal_date_stage_code "
        "ON stock_candidate_stage_signal(trade_date, stage, stock_code)"
    )


def _parse_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _within_stage_window(stage: str, trade_date: str, as_of: datetime) -> bool:
    if as_of.date().isoformat() != trade_date:
        return False
    current = as_of.time().replace(tzinfo=None)
    if stage == "premarket_pool":
        return time(0, 0) <= current < time(9, 15)
    if stage == "auction_confirmation":
        return time(9, 15) <= current <= time(9, 30)
    if stage == "intraday_strength":
        return (time(9, 30) <= current <= time(11, 30)) or (
            time(13, 0) <= current < time(14, 50)
        )
    if stage == "close_decision":
        return time(14, 50) <= current <= time(23, 59, 59)
    return False


def _readiness_cutoff_ok(readiness: dict, as_of: datetime) -> bool:
    cutoff = as_of.replace(tzinfo=None)
    for group in readiness.get("groups", []):
        if not group.get("ready"):
            return False
        selected = next(
            (
                item
                for item in group.get("relations", [])
                if item.get("relation") == group.get("selected_relation")
            ),
            None,
        )
        if not selected or not selected.get("latest_timestamp"):
            return False
        try:
            observed = datetime.fromisoformat(str(selected["latest_timestamp"])).replace(tzinfo=None)
        except ValueError:
            return False
        if observed > cutoff:
            return False
    return True


def _previous_context_date(con: duckdb.DuckDBPyConnection, trade_date: str) -> str | None:
    if not table_exists(con, "stock_candidate_score"):
        return None
    row = con.execute(
        "SELECT max(CAST(trade_date AS VARCHAR)) FROM stock_candidate_score "
        "WHERE CAST(trade_date AS VARCHAR) < ?",
        [trade_date],
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _premarket_candidates(
    con: duckdb.DuckDBPyConnection, trade_date: str, limit: int
) -> tuple[str | None, list[dict]]:
    source_date = _previous_context_date(con, trade_date)
    if not source_date:
        return None, []
    rows = _fetch_dicts(
        con,
        """
        SELECT stock_code, stock_name, score, sector_code, evidence_json
        FROM stock_candidate_score
        WHERE CAST(trade_date AS VARCHAR) = ?
        ORDER BY score DESC NULLS LAST, stock_code
        LIMIT ?
        """,
        [source_date, int(limit)],
    )
    for row in rows:
        row["source_score"] = float(row.get("score") or 0)
        row["stage_score"] = float(row.get("score") or 0)
        row["stage_decision"] = "pool"
        row["reference_price"] = None
        row["reference_price_type"] = None
    return source_date, rows


def _stage_source_rows(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    stages: tuple[str, ...],
    limit: int,
) -> list[dict]:
    placeholders = ",".join("?" for _ in stages)
    return _fetch_dicts(
        con,
        f"""
        SELECT stock_code, stock_name, score AS source_score, evidence_json
        FROM stock_candidate_stage_signal
        WHERE CAST(trade_date AS VARCHAR) = ?
          AND stage IN ({placeholders})
          AND coalesce(is_actionable, false) = true
        ORDER BY score DESC NULLS LAST, stock_code
        LIMIT ?
        """,
        [trade_date, *stages, int(limit)],
    )


def _auction_candidates(
    con: duckdb.DuckDBPyConnection, trade_date: str, limit: int, as_of: datetime
) -> tuple[str, list[dict]]:
    rows = _stage_source_rows(con, trade_date, ("premarket_pool",), limit)
    output = []
    for row in rows:
        evidence = _fetch_dicts(
            con,
            """
            SELECT auction_strength, confirmation, source_table, is_fallback, fetched_at
            FROM v_auction_status
            WHERE trade_date = ? AND (stock_code = ? OR stock_code IS NULL)
              AND fetched_at <= ?
            ORDER BY CASE WHEN stock_code = ? THEN 0 ELSE 1 END, fetched_at DESC NULLS LAST
            LIMIT 1
            """,
            [trade_date, row["stock_code"], as_of, row["stock_code"]],
        )
        auction = evidence[0] if evidence else {}
        score = float(row.get("source_score") or 0) + float(auction.get("auction_strength") or 0)
        score += -10.0 if auction.get("is_fallback") is not False else 5.0
        row.update(
            {
                "stage_score": max(0.0, min(100.0, score)),
                "stage_decision": "confirm" if score >= 65 else "watch",
                "stage_evidence": auction,
                "reference_price": None,
                "reference_price_type": None,
            }
        )
        output.append(row)
    return trade_date, output


def _aggregate_stock_source_as_of(
    con: duckdb.DuckDBPyConnection,
    table: str,
    trade_date: str,
    stock_code: str,
    as_of: datetime,
    metrics: dict[str, str],
) -> dict | None:
    if not table_exists(con, table):
        return None
    columns = set(table_columns(con, table))
    date_column = "date" if "date" in columns else "source_date" if "source_date" in columns else None
    if not date_column or not {"stock_code", "fetched_at"}.issubset(columns):
        return None
    predicates = [f"CAST({date_column} AS VARCHAR) = ?", "stock_code = ?", "fetched_at <= ?"]
    params: list[Any] = [trade_date, stock_code, as_of]
    source_time_expr = "CAST(NULL AS VARCHAR)"
    if "time" in columns:
        parsed_time = (
            "regexp_extract(CAST(time AS VARCHAR), "
            "'([0-2][0-9]:[0-5][0-9])', 1)"
        )
        predicates.append(f"{parsed_time} <> '' AND {parsed_time} <= ?")
        params.append(as_of.strftime("%H:%M"))
        source_time_expr = f"max({parsed_time})"
    metric_sql = ", ".join(
        f"{expression} AS {name}" for name, expression in metrics.items()
    )
    row = _fetch_dicts(
        con,
        f"""
        SELECT count(*) AS source_rows, max(fetched_at) AS latest_fetched_at,
               {source_time_expr} AS latest_source_time,
               {metric_sql}
        FROM {table}
        WHERE {' AND '.join(predicates)}
        """,
        params,
    )[0]
    return row if int(row.get("source_rows") or 0) > 0 else None


def _intraday_evidence_as_of(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    stock_code: str,
    as_of: datetime,
) -> dict:
    definitions = (
        (
            "l2_stock_intraday",
            {
                "intraday_high": ("max(price)", {"price"}),
                "active_fund_net": ("max(main_fund_net)", {"main_fund_net"}),
                "intraday_turnover": ("sum(turnover)", {"turnover"}),
            },
        ),
        ("l2_stock_bigorder", {"big_net_amount": ("sum(big_net_amount)", {"big_net_amount"})}),
        (
            "advanced_zjmm_min",
            {
                "zjmm_main_net_inflow": ("sum(main_net_inflow)", {"main_net_inflow"}),
                "zjmm_super_net_inflow": ("sum(super_net_inflow)", {"super_net_inflow"}),
                "zjmm_big_net_inflow": ("sum(big_net_inflow)", {"big_net_inflow"}),
            },
        ),
        ("advanced_dadan_kline", {"dadan_big_net_amount": ("sum(big_net_amount)", {"big_net_amount"})}),
        ("advanced_main_activity_kline", {"main_activity_score": ("max(main_activity_score)", {"main_activity_score"})}),
        (
            "advanced_pankou",
            {
                "pankou_net_volume": (
                    "sum(coalesce(buy1_volume, 0) - coalesce(sell1_volume, 0))",
                    {"buy1_volume", "sell1_volume"},
                )
            },
        ),
        ("l2_tick_history", {"tick_volume": ("sum(volume)", {"volume"})}),
        ("l2_tick_orders", {"tick_order_volume": ("sum(volume)", {"volume"})}),
        ("l2_tick_orders_all", {"tick_all_volume": ("sum(volume)", {"volume"})}),
        # The current-session Eastmoney clist snapshot is a real, timestamped
        # provider-main-net observation.  It is not a substitute for tick/L2
        # microstructure, but it is valid individual-stock flow evidence when
        # the latter is unavailable.
        (
            "multi_source_stock_flow",
            {
                "stock_flow_main_net": ("max(main_net)", {"main_net"}),
                "stock_flow_change_pct": ("max(change_pct)", {"change_pct"}),
                "stock_flow_close": ("max(close)", {"close"}),
                "source_provider": ("max(provider)", {"provider"}),
                "ask_price": ("max(ask_price)", {"ask_price"}),
                "ask_volume": ("max(ask_volume)", {"ask_volume"}),
            },
        ),
    )
    evidence: dict[str, Any] = {"source_tables": [], "is_fallback": True}
    latest_fetches = []
    for table, metrics in definitions:
        columns = set(table_columns(con, table)) if table_exists(con, table) else set()
        usable_metrics = {
            name: expression
            for name, (expression, required_columns) in metrics.items()
            if required_columns.issubset(columns)
        }
        if not usable_metrics:
            continue
        row = _aggregate_stock_source_as_of(
            con, table, trade_date, stock_code, as_of, usable_metrics
        )
        if not row:
            continue
        evidence["source_tables"].append(table)
        evidence[f"{table}_rows"] = int(row.pop("source_rows") or 0)
        latest = row.pop("latest_fetched_at", None)
        source_time = row.pop("latest_source_time", None)
        if latest is not None:
            latest_fetches.append(latest)
        if source_time:
            evidence[f"{table}_latest_source_time"] = source_time
        evidence.update({key: value for key, value in row.items() if value is not None})
    evidence["source_tables"] = "+".join(evidence["source_tables"])
    evidence["latest_fetched_at"] = str(max(latest_fetches)) if latest_fetches else None
    evidence["is_fallback"] = not bool(evidence["source_tables"])
    score = (
        float(evidence.get("active_fund_net") or 0) / 10_000_000.0
        + float(evidence.get("stock_flow_main_net") or 0) / 10_000_000.0
        + float(evidence.get("big_net_amount") or 0) / 10_000_000.0
        + float(evidence.get("zjmm_main_net_inflow") or 0) / 10_000_000.0
        + float(evidence.get("dadan_big_net_amount") or 0) / 10_000_000.0
        + float(evidence.get("intraday_turnover") or 0) / 100_000_000.0
        + float(evidence.get("pankou_net_volume") or 0) / 100_000.0
        + float(evidence.get("tick_volume") or 0) / 100_000.0
        + float(evidence.get("tick_order_volume") or 0) / 100_000.0
        + float(evidence.get("tick_all_volume") or 0) / 100_000.0
        + float(evidence.get("main_activity_score") or 0)
    )
    evidence["capital_flow_score"] = round(score, 4)
    evidence["strength_score"] = round(max(0.0, min(100.0, score)), 4)
    return evidence


def _intraday_candidates(
    con: duckdb.DuckDBPyConnection, trade_date: str, limit: int, as_of: datetime
) -> tuple[str, list[dict]]:
    rows = _stage_source_rows(
        con, trade_date, ("auction_confirmation", "premarket_pool"), limit
    )
    # If the process starts after the auction window, there may be no prior
    # stage rows even though the same-day realtime candidate pool exists.
    # Carry those research candidates into the intraday evidence gate; the
    # individual evidence check below still blocks them when current flow/L2
    # data is absent, so this never creates a false executable signal.
    if not rows and table_exists(con, "stock_candidate_score"):
        rows = _fetch_dicts(
            con,
            """
            SELECT stock_code, stock_name, score AS source_score, evidence_json
            FROM stock_candidate_score
            WHERE CAST(trade_date AS VARCHAR) = ?
            ORDER BY score DESC NULLS LAST, stock_code
            LIMIT ?
            """,
            [trade_date, int(limit)],
        )
    if not rows and table_exists(con, "v_limit_pool"):
        # A realtime pool can be available before the close-stage score job
        # runs.  Carry the verified same-date pool directly into the intraday
        # evidence gate; the individual stock-flow evidence still decides
        # whether a row is actionable.
        rows = _fetch_dicts(
            con,
            """
            SELECT stock_code, stock_name,
                   (45 + coalesce(board_level, 1) * 12) AS source_score,
                   NULL AS evidence_json
            FROM v_limit_pool
            WHERE trade_date = ?
            ORDER BY board_level DESC NULLS LAST, stock_code
            LIMIT ?
            """,
            [trade_date, int(limit)],
        )
    seen: set[str] = set()
    output = []
    for row in rows:
        code = str(row["stock_code"])
        if code in seen:
            continue
        seen.add(code)
        intraday = _intraday_evidence_as_of(con, trade_date, code, as_of)
        score = float(row.get("source_score") or 0) * 0.45 + float(
            intraday.get("strength_score") or 0
        ) * 0.55
        row.update(
            {
                "stage_score": max(0.0, min(100.0, score)),
                "stage_decision": "follow" if score >= 70 else "watch",
                "stage_evidence": intraday,
                "reference_price": intraday.get("stock_flow_close"),
                "reference_price_type": "eastmoney_flow_snapshot" if intraday.get("stock_flow_close") else None,
            }
        )
        output.append(row)
    return trade_date, output[:limit]


def _close_candidates(
    con: duckdb.DuckDBPyConnection, trade_date: str, limit: int
) -> tuple[str, list[dict]]:
    rows = _stage_source_rows(con, trade_date, ("intraday_strength",), limit)
    if not rows and table_exists(con, "stock_candidate_score"):
        rows = _fetch_dicts(
            con,
            """
            SELECT stock_code, stock_name, score AS source_score, evidence_json
            FROM stock_candidate_score
            WHERE CAST(trade_date AS VARCHAR) = ?
            ORDER BY score DESC NULLS LAST, stock_code
            LIMIT ?
            """,
            [trade_date, int(limit)],
        )
    output = []
    for row in rows:
        prices = _fetch_dicts(
            con,
            """
            SELECT open, close, change_pct, fetched_at
            FROM v_kline_daily
            WHERE trade_date = ? AND stock_code = ? AND ktype = 'D'
            LIMIT 1
            """,
            [trade_date, row["stock_code"]],
        )
        price = prices[0] if prices else {}
        score = float(row.get("source_score") or 0)
        row.update(
            {
                "stage_score": max(0.0, min(100.0, score)),
                "stage_decision": "keep" if score >= 70 else "reduce",
                "stage_evidence": price,
                "reference_price": price.get("close"),
                "reference_price_type": "signal_close",
            }
        )
        output.append(row)
    return trade_date, output


def _stage_readiness(
    con: duckdb.DuckDBPyConnection,
    stage: str,
    trade_date: str,
    source_trade_date: str | None,
    freshness_seconds: int | None = None,
) -> dict:
    if stage == "premarket_pool":
        if not source_trade_date:
            return {
                "trade_date": trade_date,
                "stage": "premarket",
                "ready": False,
                "missing_groups": ["previous_context"],
                "groups": [],
            }
        result = assess_trade_date_readiness(
            con,
            source_trade_date,
            "premarket",
            required_groups=("market_state", "kline", "candidate_pool"),
            max_age_seconds=freshness_seconds,
        )
        result["target_trade_date"] = trade_date
        return result
    if stage == "auction_confirmation":
        return assess_trade_date_readiness(
            con, trade_date, "auction", required_groups=("auction",)
            , max_age_seconds=freshness_seconds
        )
    if stage == "intraday_strength":
        return assess_trade_date_readiness(
            con,
            trade_date,
            "intraday",
            required_groups=("sector_capital_flow", "stock_capital_flow"),
            max_age_seconds=freshness_seconds,
        )
    return assess_trade_date_readiness(con, trade_date, "close", max_age_seconds=freshness_seconds)


def _row_evidence_actionable(
    stage: str,
    row: dict,
    *,
    strict_tradability: bool = False,
) -> tuple[bool, str | None]:
    """Require executable evidence for the individual stock, not only the market day."""
    if stage == "premarket_pool":
        return True, None
    evidence = row.get("stage_evidence") or {}
    if stage == "auction_confirmation":
        if not evidence:
            return False, "missing_stock_auction_evidence"
        if evidence.get("is_fallback") is not False:
            return False, "fallback_stock_auction_evidence"
        return True, None
    if stage == "intraday_strength":
        if not evidence:
            return False, "missing_stock_intraday_evidence"
        if evidence.get("is_fallback") is not False:
            return False, "fallback_stock_intraday_evidence"
        if strict_tradability:
            if evidence.get("stock_flow_close") is None:
                return False, "missing_executable_reference_price"
            if evidence.get("stock_flow_main_net") is None:
                return False, "missing_main_flow_value"
            if float(evidence.get("stock_flow_main_net") or 0) <= 0:
                return False, "main_flow_not_positive"
            if "eastmoney_intraday_clist_delay" in str(evidence.get("source_provider") or ""):
                return False, "delayed_provider_not_executable"
            if not evidence.get("ask_price") or not evidence.get("ask_volume"):
                return False, "missing_sell_side_liquidity"
        return True, None
    reference_price = row.get("reference_price")
    if reference_price is None or float(reference_price) <= 0:
        return False, "missing_stock_close_price"
    if strict_tradability and row.get("reference_price_type") in {None, "signal_close"}:
        return False, "close_signal_is_not_entry_executable"
    return True, None


def generate_stage_signals(
    db_path: str | Path,
    trade_date: str,
    stage: str,
    *,
    as_of_time: str | datetime | None = None,
    run_id: str = "manual",
    limit: int = 20,
    freshness_seconds: int | None = None,
    strict_tradability: bool = False,
) -> dict:
    if stage not in STAGE_NAMES:
        raise ValueError(f"Unsupported stage: {stage}")
    as_of = _parse_datetime(as_of_time)
    effective_run_id = f"{run_id}@{as_of.strftime('%Y%m%dT%H%M%S')}"
    con = duckdb.connect(str(db_path))
    try:
        ensure_stage_signal_schema(con)
        if stage == "premarket_pool":
            source_date, rows = _premarket_candidates(con, trade_date, limit)
        elif stage == "auction_confirmation":
            source_date, rows = _auction_candidates(con, trade_date, limit, as_of)
        elif stage == "intraday_strength":
            source_date, rows = _intraday_candidates(con, trade_date, limit, as_of)
        else:
            source_date, rows = _close_candidates(con, trade_date, limit)

        readiness = _stage_readiness(
            con,
            stage,
            trade_date,
            source_date,
            freshness_seconds=freshness_seconds,
        )
        within_window = _within_stage_window(stage, trade_date, as_of)
        cutoff_ok = _readiness_cutoff_ok(readiness, as_of)
        actionable_context = bool(readiness.get("ready") and within_window and cutoff_ok)

        # The intraday scheduler wakes during the lunch break and after the
        # intraday decision window.  Do not replace a valid morning snapshot
        # with 20 ``blocked_data_quality`` rows merely because no new signal
        # may be issued at that wall-clock time.  A first run with no prior
        # rows still stores the honest blocked evidence used by the existing
        # outside-window contract.
        if stage == "intraday_strength" and not within_window:
            preserved = _fetch_dicts(
                con,
                "SELECT count(*) AS rows, sum(CASE WHEN coalesce(is_actionable,false) THEN 1 ELSE 0 END) AS actionable "
                "FROM stock_candidate_stage_signal WHERE trade_date=? AND stage=?",
                [trade_date, stage],
            )[0]
            if int(preserved.get("rows") or 0) > 0:
                return {
                    "trade_date": trade_date,
                    "stage": stage,
                    "source_trade_date": source_date,
                    "as_of_time": as_of.isoformat(timespec="seconds"),
                    "within_stage_window": within_window,
                    "source_cutoff_ok": cutoff_ok,
                    "readiness": readiness,
                    "inserted": 0,
                    "actionable": int(preserved.get("actionable") or 0),
                    "preserved": True,
                    "feature_version": FEATURE_VERSION,
                }

        con.execute("BEGIN TRANSACTION")
        try:
            inserted = 0
            actionable = 0
            for row in rows:
                score = max(0.0, min(100.0, float(row.get("stage_score") or 0)))
                row_ready, row_block_reason = _row_evidence_actionable(
                    stage,
                    row,
                    strict_tradability=strict_tradability,
                )
                is_actionable = bool(actionable_context and row_ready)
                signal_triggered = str(row.get("stage_decision") or "").lower() in {
                    "pool", "confirm", "follow", "keep", "buy", "probe"
                }
                data_complete = bool(actionable_context)
                tradable = bool(row_ready)
                # Risk approval is deliberately left to the portfolio/risk
                # loop.  is_executable means execution evidence is complete,
                # not that a position has been approved.
                is_executable = bool(data_complete and signal_triggered and tradable)
                decision = (
                    str(row.get("stage_decision") or "watch")
                    if is_actionable
                    else "blocked_data_quality"
                )
                evidence = {
                    "stage": stage,
                    "target_trade_date": trade_date,
                    "source_trade_date": source_date,
                    "as_of_time": as_of.isoformat(timespec="seconds"),
                    "within_stage_window": within_window,
                    "source_cutoff_ok": cutoff_ok,
                    "input_cutoff_enforced": True,
                    "row_evidence_ready": row_ready,
                    "row_block_reason": row_block_reason,
                    "source_score": row.get("source_score"),
                    "stage_evidence": row.get("stage_evidence") or {},
                    "prior_evidence_json": row.get("evidence_json"),
                    "feature_version": FEATURE_VERSION,
                }
                con.execute(
                    """
                    INSERT OR REPLACE INTO stock_candidate_stage_signal (
                        trade_date, stage, stock_code, stock_name, score, decision,
                        evidence_json, source_trade_date, as_of_time, run_id,
                        is_actionable, readiness_json, reference_price,
                        reference_price_type, feature_version
                        ,data_complete, signal_triggered, tradable, risk_approved, is_executable
                    )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
                    """,
                    [
                        trade_date,
                        stage,
                        row.get("stock_code"),
                        row.get("stock_name"),
                        score,
                        decision,
                        json.dumps(evidence, ensure_ascii=False, default=str),
                        source_date,
                        as_of,
                        effective_run_id,
                        is_actionable,
                        json.dumps(readiness, ensure_ascii=False, default=str),
                        row.get("reference_price"),
                        row.get("reference_price_type"),
                        FEATURE_VERSION,
                        data_complete,
                        signal_triggered,
                        tradable,
                        False,
                        is_executable,
                    ],
                )
                inserted += 1
                actionable += int(is_actionable)
            if inserted:
                con.execute(
                    "DELETE FROM stock_candidate_stage_signal "
                    "WHERE trade_date = ? AND stage = ? AND coalesce(run_id, '') != ?",
                    [trade_date, stage, effective_run_id],
                )
            else:
                con.execute(
                    "DELETE FROM stock_candidate_stage_signal WHERE trade_date = ? AND stage = ?",
                    [trade_date, stage],
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return {
        "trade_date": trade_date,
        "stage": stage,
        "source_trade_date": source_date,
        "as_of_time": as_of.isoformat(timespec="seconds"),
        "within_stage_window": within_window,
        "source_cutoff_ok": cutoff_ok,
        "readiness": readiness,
        "inserted": inserted,
        "actionable": actionable,
        "feature_version": FEATURE_VERSION,
    }
