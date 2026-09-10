"""As-of-time stage signal generation without same-day look-ahead leakage."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import json
from pathlib import Path
from typing import Any

import duckdb

import math

from trade_system.executable_quotes import (
    is_delayed_provider,
    load_executable_quote,
)
from trade_system.flow_ranking import stock_provider_rank
from trade_system.quality import table_columns, table_exists
from trade_system.readiness import assess_trade_date_readiness
from trade_system.db_utils import fetch_dicts as _fetch_dicts
from trade_system.kline_access import canonical_daily_kline_relation


STAGE_NAMES = (
    "premarket_pool",
    "auction_confirmation",
    "intraday_strength",
    "close_decision",
)

FEATURE_VERSION = "stage_v3_score_recal"

# Intraday composite score: prior-stage quality vs same-session strength.
INTRADAY_SOURCE_WEIGHT = 0.40
INTRADAY_STRENGTH_WEIGHT = 0.60
# Follow bars after recalibration (log-scaled flow maps p50≈24, p90≈50, top≈85).
INTRADAY_FOLLOW_THRESHOLD = 62.0
INTRADAY_FOLLOW_THRESHOLD_LIVE = 58.0



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
            ,execution_valid_until TIMESTAMP
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
        "execution_valid_until": "TIMESTAMP",
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


def _execution_valid_until(stage: str, as_of: datetime) -> datetime | None:
    """Return the last moment at which a same-session entry may be used."""
    if stage == "auction_confirmation":
        return datetime.combine(as_of.date(), time(9, 30))
    if stage != "intraday_strength":
        return None
    session_end = time(11, 30) if as_of.time() < time(12, 0) else time(14, 50)
    hard_end = datetime.combine(as_of.date(), session_end)
    return min(as_of + timedelta(minutes=10), hard_end)


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
        # Allow a few seconds of clock/write lag: collectors often stamp
        # fetched_at slightly after the stage as_of snapshot is taken.
        if (observed - cutoff).total_seconds() > 5.0:
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
    source_date = trade_date
    # The auction watcher can start after a reboot or before the premarket
    # score job has populated ``premarket_pool``.  Carry the verified same-day
    # candidate sources forward instead of silently producing zero rows.  The
    # auction evidence and tradability gates below still decide whether any row
    # is actionable; this fallback only preserves the research universe.
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
    if not rows:
        # Last resort: use the prior-session research pool.  It remains clearly
        # identified by source_trade_date and cannot pass a same-day evidence
        # gate unless today's auction snapshot is available.
        source_date, rows = _premarket_candidates(con, trade_date, limit)
    output = []
    for row in rows:
        evidence = _fetch_dicts(
            con,
            """
            SELECT auction_strength, confirmation, source_table, is_fallback, fetched_at
            FROM v_auction_status
            WHERE trade_date = ? AND stock_code = ?
              AND fetched_at <= ?
            ORDER BY fetched_at DESC NULLS LAST
            LIMIT 1
            """,
            [trade_date, row["stock_code"], as_of],
        )
        auction = evidence[0] if evidence else {}
        score = float(row.get("source_score") or 0) + float(auction.get("auction_strength") or 0)
        # Only a real per-stock auction row earns the bonus; a market-wide
        # summary row (stock_code IS NULL) must never be inherited as the
        # stock's own auction strength.  No row at all is also a penalty.
        score += 5.0 if auction.get("is_fallback") is False else -10.0
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
    return source_date, output


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


def _canonical_stock_flow_as_of(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    stock_code: str,
    as_of: datetime,
) -> dict | None:
    """Pick one multi_source_stock_flow row by provider priority (not max())."""
    if not table_exists(con, "multi_source_stock_flow"):
        return None
    columns = set(table_columns(con, "multi_source_stock_flow"))
    if not {"source_date", "stock_code", "main_net", "provider", "fetched_at"}.issubset(columns):
        return None
    close_expr = "close" if "close" in columns else "NULL"
    chg_expr = "change_pct" if "change_pct" in columns else "NULL"
    ask_p = "ask_price" if "ask_price" in columns else "NULL"
    ask_v = "ask_volume" if "ask_volume" in columns else "NULL"
    stale_clause = (
        "AND coalesce(is_stale, false)=false" if "is_stale" in columns else ""
    )
    rows = con.execute(
        f"""
        SELECT provider, main_net, {close_expr}, {chg_expr}, {ask_p}, {ask_v}, fetched_at
        FROM multi_source_stock_flow
        WHERE CAST(source_date AS VARCHAR)=?
          AND stock_code=?
          AND fetched_at <= ?
          AND main_net IS NOT NULL
          {stale_clause}
        """,
        [trade_date, stock_code, as_of],
    ).fetchall()
    if not rows:
        return None
    best = None
    best_key = None
    for provider, main_net, close, chg, ask_price, ask_volume, fetched_at in rows:
        # Prefer higher provider rank, then non-null close, then fresher fetch.
        key = (
            stock_provider_rank(provider),
            close is not None,
            fetched_at or datetime.min,
        )
        if best is None or key > best_key:
            best_key = key
            best = {
                "source_provider": provider,
                "stock_flow_main_net": main_net,
                "stock_flow_close": close,
                "stock_flow_change_pct": chg,
                "ask_price": ask_price,
                "ask_volume": ask_volume,
                "latest_fetched_at": fetched_at,
                "source_rows": 1,
            }
    return best


def _attach_executable_price(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    stock_code: str,
    as_of: datetime,
    evidence: dict,
) -> None:
    """Attach a same-session entry reference that is not a delayed clist price.

    Priority:
    1. multi_source non-delay close (live clist / kpl / tushare)
    2. executable_quote_snapshot (Tencent spot)
    3. l2_stock_intraday last price
    """
    provider = evidence.get("source_provider")
    close = evidence.get("stock_flow_close")
    if close is not None and not is_delayed_provider(provider):
        evidence["executable_price"] = close
        evidence["executable_provider"] = provider
        evidence["executable_price_type"] = "stock_flow_live"
        return

    quote = load_executable_quote(con, trade_date, stock_code, as_of=as_of)
    if quote and quote.get("price") is not None and float(quote["price"]) > 0:
        evidence["executable_price"] = float(quote["price"])
        evidence["executable_provider"] = quote.get("provider")
        evidence["executable_price_type"] = "tencent_spot_quote"
        evidence["ask_price"] = evidence.get("ask_price") or quote.get("ask1")
        evidence["ask_volume"] = evidence.get("ask_volume") or quote.get("ask1_vol")
        evidence["bid_price"] = quote.get("bid1")
        evidence["bid_volume"] = quote.get("bid1_vol")
        evidence.setdefault("source_tables", "")
        # source_tables may already be joined string; track side channel too.
        evidence["executable_quote_source"] = "executable_quote_snapshot"
        if quote.get("fetched_at") is not None:
            evidence["executable_fetched_at"] = str(quote["fetched_at"])
        return

    # L2 last trade as last-resort live price (not delayed clist).
    if table_exists(con, "l2_stock_intraday"):
        cols = set(table_columns(con, "l2_stock_intraday"))
        if {"date", "stock_code", "price", "fetched_at"}.issubset(cols):
            row = con.execute(
                """
                SELECT price, fetched_at FROM l2_stock_intraday
                WHERE CAST(date AS VARCHAR)=? AND stock_code=? AND fetched_at<=?
                  AND price IS NOT NULL AND price > 0
                ORDER BY fetched_at DESC NULLS LAST
                LIMIT 1
                """,
                [trade_date, stock_code, as_of],
            ).fetchone()
            if row:
                evidence["executable_price"] = float(row[0])
                evidence["executable_provider"] = "l2_stock_intraday"
                evidence["executable_price_type"] = "l2_last_price"
                evidence["executable_fetched_at"] = str(row[1]) if row[1] else None


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

    # Multi-source flow: pick a canonical provider row (live > delay).
    flow = _canonical_stock_flow_as_of(con, trade_date, stock_code, as_of)
    if flow:
        evidence["source_tables"].append("multi_source_stock_flow")
        evidence["multi_source_stock_flow_rows"] = int(flow.pop("source_rows") or 0)
        latest = flow.pop("latest_fetched_at", None)
        if latest is not None:
            latest_fetches.append(latest)
        evidence.update({k: v for k, v in flow.items() if v is not None})

    _attach_executable_price(con, trade_date, stock_code, as_of, evidence)

    evidence["source_tables"] = "+".join(evidence["source_tables"])
    evidence["latest_fetched_at"] = str(max(latest_fetches)) if latest_fetches else None
    evidence["is_fallback"] = not bool(evidence["source_tables"])
    strength = _compute_intraday_strength(evidence)
    evidence["capital_flow_score"] = strength["capital_flow_score"]
    evidence["money_score"] = strength["money_score"]
    evidence["change_score"] = strength["change_score"]
    evidence["micro_score"] = strength["micro_score"]
    evidence["strength_score"] = strength["strength_score"]
    evidence["score_version"] = FEATURE_VERSION
    return evidence


def _yuan_flow_score(yuan: float | None) -> float:
    """Map A-share main-net (yuan) onto 0–100 with a log scale.

    Empirically (2026-07-31 full-market delay clist):
      ~1.8e5 (p10) → 5,  ~1.6e6 (p50) → 24,  ~3.3e7 (p90) → 50,  ~2e9 (max) → 87
    Linear ``yuan/1e7`` left almost every name below 10 and made follow unreachable.
    """
    try:
        value = float(yuan) if yuan is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        return 0.0
    # log10(1e5)=5 → 0; each decade of yuan adds 20 points, capped at 100.
    return max(0.0, min(100.0, (math.log10(value) - 5.0) * 20.0))


def _change_pct_score(change_pct: float | None) -> float:
    """Map intraday change% onto 0–100 (neutral ~40 when missing)."""
    if change_pct is None or change_pct == "":
        return 40.0
    try:
        chg = float(change_pct)
    except (TypeError, ValueError):
        return 40.0
    # -5% → 0, 0% → 40, +5% → 80, +7.5% → 100
    return max(0.0, min(100.0, 40.0 + chg * 8.0))


def _compute_intraday_strength(evidence: dict) -> dict[str, float]:
    """Build a 0–100 same-session strength score from flow + change + micro."""
    money_score = max(
        _yuan_flow_score(evidence.get("active_fund_net")),
        _yuan_flow_score(evidence.get("stock_flow_main_net")),
        _yuan_flow_score(evidence.get("big_net_amount")),
        _yuan_flow_score(evidence.get("zjmm_main_net_inflow")),
        _yuan_flow_score(evidence.get("dadan_big_net_amount")),
    )
    change_score = _change_pct_score(evidence.get("stock_flow_change_pct"))
    # Microstructure is supportive, not dominant (units vary wildly by source).
    micro_raw = (
        float(evidence.get("intraday_turnover") or 0) / 200_000_000.0
        + float(evidence.get("pankou_net_volume") or 0) / 200_000.0
        + float(evidence.get("tick_volume") or 0) / 200_000.0
        + float(evidence.get("tick_order_volume") or 0) / 200_000.0
        + float(evidence.get("tick_all_volume") or 0) / 200_000.0
        + float(evidence.get("main_activity_score") or 0)
    )
    micro_score = max(0.0, min(40.0, micro_raw))
    # Primary: money; secondary: price action; tertiary: micro.
    strength = 0.70 * money_score + 0.25 * change_score + 0.05 * (micro_score * 2.5)
    strength = max(0.0, min(100.0, strength))
    return {
        "money_score": round(money_score, 4),
        "change_score": round(change_score, 4),
        "micro_score": round(micro_score, 4),
        "capital_flow_score": round(money_score, 4),
        "strength_score": round(strength, 4),
    }


def _compose_intraday_stage_score(
    source_score: float,
    strength_score: float,
    *,
    has_live_price: bool,
) -> tuple[float, str, float]:
    """Return (stage_score, decision, follow_threshold)."""
    live_bonus = 4.0 if has_live_price else 0.0
    score = (
        float(source_score or 0) * INTRADAY_SOURCE_WEIGHT
        + float(strength_score or 0) * INTRADAY_STRENGTH_WEIGHT
        + live_bonus
    )
    score = max(0.0, min(100.0, score))
    threshold = (
        INTRADAY_FOLLOW_THRESHOLD_LIVE if has_live_price else INTRADAY_FOLLOW_THRESHOLD
    )
    decision = "follow" if score >= threshold else "watch"
    return score, decision, threshold


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
        # Prefer a non-delay executable price for entry; fall back to flow close
        # for analytics display only.
        ref = intraday.get("executable_price")
        ref_type = intraday.get("executable_price_type")
        if ref is None and intraday.get("stock_flow_close") is not None:
            ref = intraday.get("stock_flow_close")
            ref_type = (
                "stock_flow_delayed"
                if is_delayed_provider(intraday.get("source_provider"))
                else "stock_flow_live"
            )
        has_live_px = bool(
            ref is not None
            and not is_delayed_provider(intraday.get("executable_provider"))
            and ref_type not in {None, "stock_flow_delayed"}
        )
        score, decision, follow_threshold = _compose_intraday_stage_score(
            float(row.get("source_score") or 0),
            float(intraday.get("strength_score") or 0),
            has_live_price=has_live_px,
        )
        intraday["follow_threshold"] = follow_threshold
        intraday["has_live_price"] = has_live_px
        row.update(
            {
                "stage_score": score,
                "stage_decision": decision,
                "stage_evidence": intraday,
                "reference_price": ref,
                "reference_price_type": ref_type,
            }
        )
        output.append(row)
    return trade_date, output[:limit]


def _kline_same_date_ready(con: duckdb.DuckDBPyConnection, trade_date: str) -> bool:
    """True when normalized daily bars exist for the trade date."""
    if table_exists(con, "tushare_daily") or table_exists(con, "v_kline_daily"):
        try:
            kline_relation = canonical_daily_kline_relation(con)
            count = con.execute(
                f"SELECT count(*) FROM {kline_relation} "
                "WHERE CAST(trade_date AS VARCHAR)=? AND close IS NOT NULL",
                [trade_date],
            ).fetchone()[0]
            if int(count or 0) > 0:
                return True
        except Exception:
            pass
    if table_exists(con, "tushare_daily"):
        try:
            count = con.execute(
                "SELECT count(*) FROM tushare_daily "
                "WHERE CAST(date AS VARCHAR)=? AND close IS NOT NULL",
                [trade_date],
            ).fetchone()[0]
            return int(count or 0) > 0
        except Exception:
            return False
    return False


def _close_signals_need_refresh(con: duckdb.DuckDBPyConnection, trade_date: str) -> bool:
    """Re-run close when prior rows are pending/missing price but kline is now ready."""
    if not table_exists(con, "stock_candidate_stage_signal"):
        return _kline_same_date_ready(con, trade_date)
    if not _kline_same_date_ready(con, trade_date):
        return False
    try:
        row = con.execute(
            """
            SELECT count(*) AS total,
                   sum(CASE
                         WHEN coalesce(decision, '') IN ('pending_provider', 'blocked_data_quality')
                              AND (
                                  coalesce(reference_price, 0) <= 0
                                  OR coalesce(json_extract_string(evidence_json, '$.row_block_reason'), '')
                                     = 'missing_stock_close_price'
                                  OR coalesce(json_extract_string(evidence_json, '$.source_cutoff_ok'), 'true')
                                     = 'false'
                              )
                         THEN 1 ELSE 0 END) AS pending_or_missing,
                   sum(CASE WHEN coalesce(is_actionable, false) THEN 1 ELSE 0 END) AS actionable
            FROM stock_candidate_stage_signal
            WHERE CAST(trade_date AS VARCHAR)=? AND stage='close_decision'
            """,
            [trade_date],
        ).fetchone()
    except Exception:
        return True
    total = int(row[0] or 0)
    pending = int(row[1] or 0)
    actionable = int(row[2] or 0)
    if total == 0:
        return True
    if actionable > 0 and pending == 0:
        return False
    return pending > 0


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
    kline_relation = canonical_daily_kline_relation(con)
    output = []
    for row in rows:
        prices = _fetch_dicts(
            con,
            f"""
            SELECT open, close, change_pct, fetched_at, source_table
            FROM {kline_relation}
            WHERE CAST(trade_date AS VARCHAR) = ?
              AND stock_code = ?
              AND upper(coalesce(ktype, 'D')) = 'D'
              AND close IS NOT NULL
            LIMIT 1
            """,
            [trade_date, row["stock_code"]],
        )
        if not prices and table_exists(con, "tushare_daily"):
            prices = _fetch_dicts(
                con,
                """
                SELECT open, close, change_pct, fetched_at, 'tushare_daily' AS source_table
                FROM tushare_daily
                WHERE CAST(date AS VARCHAR) = ? AND stock_code = ? AND close IS NOT NULL
                LIMIT 1
                """,
                [trade_date, row["stock_code"]],
            )
        price = prices[0] if prices else {}
        score = float(row.get("source_score") or 0)
        close_px = price.get("close")
        try:
            close_val = float(close_px) if close_px is not None else None
        except (TypeError, ValueError):
            close_val = None
        row.update(
            {
                "stage_score": max(0.0, min(100.0, score)),
                "stage_decision": "keep" if score >= 70 else "reduce",
                "stage_evidence": price,
                "reference_price": close_val,
                "reference_price_type": "signal_close" if close_val is not None else None,
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
            # Entry requires a non-delay same-session price.  Flow may still
            # come from the delayed clist for ranking, but the price used for
            # execution must be live (tencent spot / live clist / kpl / L2).
            exec_price = evidence.get("executable_price")
            exec_provider = evidence.get("executable_provider") or evidence.get(
                "source_provider"
            )
            if exec_price is None:
                # Legacy path: only accept stock_flow_close when its provider
                # is not delayed.
                if evidence.get("stock_flow_close") is None:
                    return False, "missing_executable_reference_price"
                if is_delayed_provider(evidence.get("source_provider")):
                    return False, "delayed_provider_not_executable"
                exec_price = evidence.get("stock_flow_close")
                exec_provider = evidence.get("source_provider")
            try:
                if float(exec_price) <= 0:
                    return False, "missing_executable_reference_price"
            except (TypeError, ValueError):
                return False, "missing_executable_reference_price"
            if is_delayed_provider(exec_provider):
                return False, "delayed_provider_not_executable"
            if evidence.get("stock_flow_main_net") is None:
                return False, "missing_main_flow_value"
            if float(evidence.get("stock_flow_main_net") or 0) <= 0:
                return False, "main_flow_not_positive"
            # A buy candidate must have real sell-side liquidity.  A limit-up
            # quote with ask=0 is observable but not entry-executable.
            ask_px = evidence.get("ask_price")
            ask_vol = evidence.get("ask_volume")
            try:
                ask_px_f = float(ask_px) if ask_px not in (None, "") else None
            except (TypeError, ValueError):
                ask_px_f = None
            try:
                ask_vol_f = float(ask_vol) if ask_vol not in (None, "") else None
            except (TypeError, ValueError):
                ask_vol_f = None
            if (
                ask_px_f is None
                or ask_px_f <= 0
                or ask_vol_f is None
                or ask_vol_f <= 0
            ):
                return False, "missing_sell_side_liquidity"
        return True, None
    # close_decision: same-day close is review evidence (keep/reduce), not a
    # same-session entry price.  Do not fail row_ready solely because the
    # reference is ``signal_close`` — that used to zero out every close candidate
    # even after TuShare filled.  Entry executability is decided separately.
    reference_price = row.get("reference_price")
    if reference_price is None:
        return False, "missing_stock_close_price"
    try:
        if float(reference_price) <= 0:
            return False, "missing_stock_close_price"
    except (TypeError, ValueError):
        return False, "missing_stock_close_price"
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
    con = None
    last_open_err: Exception | None = None
    for attempt in range(20):
        try:
            from trade_system.db_utils import legacy_connect
            con = legacy_connect(str(db_path))
            break
        except Exception as exc:
            last_open_err = exc
            # Scheduler may hold an exclusive lock during phase ticks.
            import time as _time

            _time.sleep(min(3.0, 0.4 * (attempt + 1)))
    if con is None:
        raise last_open_err or RuntimeError(f"cannot open {db_path}")
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

        within_window = _within_stage_window(stage, trade_date, as_of)
        # Close may regenerate after TuShare lag fills (often after 17:30 or next
        # pre-open).  Treat that as an in-window refresh when prior rows are still
        # pending on missing close prices and kline is now present.
        close_refresh = bool(
            stage == "close_decision" and _close_signals_need_refresh(con, trade_date)
        )
        # Refresh must not inherit a stale max-age gate: provider fill timestamps
        # legitimately land hours after the first close pass.
        readiness = _stage_readiness(
            con,
            stage,
            trade_date,
            source_date,
            freshness_seconds=None if close_refresh else freshness_seconds,
        )
        cutoff_ok = _readiness_cutoff_ok(readiness, as_of)
        effective_window = bool(within_window or close_refresh)
        # On close refresh, skip the as-of cutoff gate: the new kline timestamp is
        # intentionally later than the original close decision clock.
        # Intraday refreshes that just collected executable quotes / L2 curves
        # also stamp fetched_at after the stage as_of; with freshness unbound
        # treat cutoff as soft so live evidence is not discarded.
        soft_cutoff = bool(
            stage == "intraday_strength" and freshness_seconds is None and within_window
        )
        effective_cutoff = bool(cutoff_ok or close_refresh or soft_cutoff)
        actionable_context = bool(
            readiness.get("ready") and effective_window and effective_cutoff
        )
        # If readiness still says kline missing but we just confirmed same-date
        # bars exist (view lag / stale readiness snapshot), allow this pending
        # close refresh to process the filled prices.  Do not mutate the
        # canonical readiness fields: a refresh exception is not full-chain
        # certification and must not make another consumer report "ready".
        if (
            stage == "close_decision"
            and not actionable_context
            and close_refresh
            and _kline_same_date_ready(con, trade_date)
        ):
            actionable_context = True
            readiness = dict(readiness or {})
            readiness["close_refresh_forced"] = True

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

        # Outside the close window with nothing to refresh: preserve prior rows.
        if stage == "close_decision" and not effective_window:
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
                    "close_refresh": False,
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
                data_complete = bool(actionable_context and row_ready)
                tradable = bool(row_ready)
                # Portfolio/risk approval is a separate mandatory gate.  Stage
                # generation can mark a strategy candidate tradable, but never
                # call it executable before the operator risk loop approves it.
                risk_approved = False
                is_executable = bool(
                    data_complete
                    and signal_triggered
                    and tradable
                    and risk_approved
                )
                # Close bar is review/keep-reduce only (T+1 next open for entry).
                if stage == "close_decision" and row.get("reference_price_type") in {
                    None,
                    "signal_close",
                }:
                    is_executable = False
                if is_actionable:
                    decision = str(row.get("stage_decision") or "watch")
                elif row_block_reason == "missing_stock_close_price":
                    # Transient provider lag (TuShare often lands after 17:30).
                    decision = "pending_provider"
                else:
                    decision = "blocked_data_quality"
                evidence = {
                    "stage": stage,
                    "target_trade_date": trade_date,
                    "source_trade_date": source_date,
                    "as_of_time": as_of.isoformat(timespec="seconds"),
                    "within_stage_window": within_window,
                    "close_refresh": close_refresh,
                    "source_cutoff_ok": cutoff_ok,
                    "input_cutoff_enforced": not close_refresh,
                    "row_evidence_ready": row_ready,
                    "row_block_reason": row_block_reason,
                    "entry_executable": is_executable,
                    "execution_valid_until": (
                        _execution_valid_until(stage, as_of).isoformat(timespec="seconds")
                        if is_executable or stage in {"auction_confirmation", "intraday_strength"}
                        else None
                    ),
                    "entry_block_reason": (
                        "close_signal_is_not_entry_executable"
                        if stage == "close_decision" and row_ready and not is_executable
                        else row_block_reason
                    ),
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
                        ,execution_valid_until
                    )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                        _execution_valid_until(stage, as_of),
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
        "close_refresh": close_refresh if stage == "close_decision" else False,
        "readiness": readiness,
        "inserted": inserted,
        "actionable": actionable,
        "feature_version": FEATURE_VERSION,
    }


def refresh_close_signals_if_needed(
    db_path: str | Path,
    trade_date: str,
    *,
    run_id: str = "close_refresh",
    limit: int = 200,
    freshness_seconds: int | None = None,
    strict_tradability: bool = True,
    as_of_time: str | datetime | None = None,
) -> dict:
    """Rebuild close_decision rows when kline arrived after the first close pass."""
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        ensure_stage_signal_schema(con)
        needed = _close_signals_need_refresh(con, trade_date)
    finally:
        con.close()
    if not needed:
        return {
            "trade_date": trade_date,
            "stage": "close_decision",
            "refreshed": False,
            "inserted": 0,
            "actionable": 0,
            "reason": "no_refresh_needed",
        }
    result = generate_stage_signals(
        db_path,
        trade_date,
        "close_decision",
        as_of_time=as_of_time or datetime.now(),
        run_id=run_id,
        limit=limit,
        freshness_seconds=freshness_seconds,
        strict_tradability=strict_tradability,
    )
    result["refreshed"] = True
    return result
