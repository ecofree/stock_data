"""Trade-date freshness and actionability gates.

The project used to treat a relation as available when it contained any row
from any date.  Trading decisions need a stricter contract: required evidence
must exist for the requested trade date (or the immediately preceding session
for pre-market context), and fallback-only evidence must remain visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import duckdb

from trade_system.quality import table_columns, table_exists
from trade_system.time_utils import as_local_naive
from trade_system.gate_contract import build_operator_state


def _normalize_trade_date(value: str) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return str(value)[:10]


@dataclass(frozen=True)
class ReadinessGroup:
    name: str
    relations: tuple[str, ...]
    allow_fallback: bool = False


GROUPS = {
    "market_state": ReadinessGroup(
        "market_state", ("v_market_state_inputs", "v_market_daily", "daily_summary")
    ),
    "kline": ReadinessGroup("kline", ("v_kline_daily", "kline")),
    # ``stock_candidate_score`` is a research fallback generated from the
    # THS snapshot when the same-day limit-up feed is absent.  It is visible
    # in readiness reports but does not satisfy the actionable gate.
    "candidate_pool": ReadinessGroup("candidate_pool", ("v_limit_pool", "stock_candidate_score")),
    "auction": ReadinessGroup(
        "auction",
        (
            "v_auction_status", "auction_tick", "auction_quote_snapshot",
            "auction_bidding_anomaly", "advanced_morning_bidding_summary",
        ),
        allow_fallback=True,
    ),
    "sector_capital_flow": ReadinessGroup(
        "sector_capital_flow", ("v_sector_capital", "sector_capital")
    ),
    "stock_capital_flow": ReadinessGroup(
        "stock_capital_flow",
        (
            "v_intraday_capital_flow_evidence",
            "multi_source_stock_flow",
            "l2_stock_intraday",
            "l2_stock_bigorder",
            "advanced_zjmm_min",
            "advanced_dadan_kline",
            "advanced_main_activity_kline",
        ),
    ),
}


STAGE_REQUIREMENTS = {
    "premarket": ("market_state", "kline", "candidate_pool"),
    "auction": ("market_state", "candidate_pool", "auction"),
    # Position sizing and risk state require a same-session market regime.
    "intraday": ("market_state", "candidate_pool", "sector_capital_flow", "stock_capital_flow"),
    "close": ("market_state", "kline", "candidate_pool", "sector_capital_flow", "stock_capital_flow"),
    "postmarket": ("market_state", "kline", "candidate_pool", "sector_capital_flow", "stock_capital_flow"),
}


def _date_column(con: duckdb.DuckDBPyConnection, relation: str) -> str | None:
    columns = set(table_columns(con, relation))
    return next((name for name in ("trade_date", "date", "source_date") if name in columns), None)


def _timestamp_column(con: duckdb.DuckDBPyConnection, relation: str) -> str | None:
    columns = set(table_columns(con, relation))
    return next(
        (name for name in ("fetched_at", "updated_at", "generated_at", "created_at") if name in columns),
        None,
    )


def _previous_open_session(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
) -> str | None:
    """Return the immediately preceding exchange session when the calendar proves it.

    Pre-open providers legitimately describe yesterday's completed market.
    We only accept that context when ``tushare_trade_cal`` identifies the
    exact preceding open session; an arbitrary old market row must never make
    auction readiness look current.
    """
    if not table_exists(con, "tushare_trade_cal"):
        return None
    columns = set(table_columns(con, "tushare_trade_cal"))
    if not {"cal_date", "is_open"} <= columns:
        return None
    try:
        row = con.execute(
            """
            SELECT max(CAST(cal_date AS DATE))
            FROM tushare_trade_cal
            WHERE CAST(cal_date AS DATE) < CAST(? AS DATE)
              AND coalesce(CAST(is_open AS BOOLEAN), false)
            """,
            [trade_date],
        ).fetchone()
    except Exception:
        return None
    return str(row[0]) if row and row[0] is not None else None


def relation_freshness(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    trade_date: str,
    *,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> dict:
    now = as_local_naive(now) or datetime.now()
    if not table_exists(con, relation):
        return {
            "relation": relation,
            "exists": False,
            "date_column": None,
            "rows": 0,
            "real_rows": 0,
            "fallback_rows": 0,
            "latest_date": None,
            "latest_timestamp": None,
            "status": "missing_relation",
        }
    date_column = _date_column(con, relation)
    if not date_column:
        return {
            "relation": relation,
            "exists": True,
            "date_column": None,
            "rows": 0,
            "real_rows": 0,
            "fallback_rows": 0,
            "latest_date": None,
            "latest_timestamp": None,
            "status": "missing_date_column",
        }
    columns = set(table_columns(con, relation))
    fallback_expr = (
        "sum(case when coalesce(is_fallback, false) then 1 else 0 end)"
        if "is_fallback" in columns
        else "sum(case when source <> 'limit_pool' then 1 else 0 end)"
        if relation == "stock_candidate_score" and "source" in columns
        else "0"
    )
    real_expr = (
        "sum(case when not coalesce(is_fallback, false) then 1 else 0 end)"
        if "is_fallback" in columns
        else "sum(case when source = 'limit_pool' then 1 else 0 end)"
        if relation == "stock_candidate_score" and "source" in columns
        else "count(*)"
    )
    timestamp_column = _timestamp_column(con, relation)
    timestamp_expr = f'max("{timestamp_column}")' if timestamp_column else "NULL"
    flow_columns = (
        ("main_net", "super_net", "large_net", "mid_net", "small_net")
        if relation == "multi_source_sector_flow"
        else ("main_net_inflow", "super_net_inflow", "big_net_inflow", "mid_net_inflow", "small_net_inflow")
        if relation == "sector_capital"
        else ()
    )
    valid_flow = (
        "(" + " OR ".join(f'"{name}" IS NOT NULL' for name in flow_columns if name in columns) + ")"
        if any(name in columns for name in flow_columns)
        else "TRUE"
    )
    # A stock-flow row with only bucket placeholders is not usable evidence.
    # Count only rows with a canonical main-order value; the raw total-net
    # value remains available through ``net_total`` for separate analysis.
    if relation == "multi_source_stock_flow" and "main_net" in columns:
        valid_flow = '"main_net" IS NOT NULL'
    as_of_filter = (
        f' AND "{timestamp_column}" <= ?'
        if now is not None and timestamp_column
        else ""
    )
    base_params: list[object] = [trade_date]
    if as_of_filter:
        base_params.append(now)
    rows, real_rows, fallback_rows, latest_timestamp = con.execute(
        f"""
        SELECT count(*), {real_expr}, {fallback_expr}, {timestamp_expr}
        FROM "{relation}"
        WHERE CAST("{date_column}" AS VARCHAR) = ? AND {valid_flow}{as_of_filter}
        """,
        base_params,
    ).fetchone()
    latest_date = con.execute(
        f'SELECT max(CAST("{date_column}" AS VARCHAR)) FROM "{relation}"'
    ).fetchone()[0]
    # Freshness is a row-level contract.  A single newly written row must not
    # make an otherwise old/partial snapshot look current.
    if max_age_seconds is not None and timestamp_column and latest_timestamp is not None:
        try:
            observed_at = latest_timestamp
            if isinstance(observed_at, str):
                observed_at = as_local_naive(observed_at)
            cutoff = (now or datetime.now()) - timedelta(seconds=max(0, int(max_age_seconds)))
            rows, real_rows, fallback_rows = con.execute(
                f"""
                SELECT count(*), {real_expr}, {fallback_expr}
                FROM "{relation}"
                WHERE CAST("{date_column}" AS VARCHAR) = ?
                  AND {valid_flow}
                  AND "{timestamp_column}" >= ?
                  AND "{timestamp_column}" <= ?
                """,
                [trade_date, cutoff, now or datetime.now()],
            ).fetchone()
        except (TypeError, ValueError):
            pass
    status = "ready" if rows and real_rows else "fallback_only" if rows else "stale_or_empty"
    freshness_age_seconds = None
    if rows and timestamp_column and latest_timestamp is not None:
        try:
            observed_at = latest_timestamp
            if isinstance(observed_at, str):
                observed_at = as_local_naive(observed_at)
            reference_now = now or datetime.now()
            freshness_age_seconds = max(0.0, (reference_now - observed_at).total_seconds())
        except (TypeError, ValueError):
            freshness_age_seconds = None
    if rows and max_age_seconds is not None:
        if not timestamp_column or latest_timestamp is None:
            status = "missing_timestamp"
        elif freshness_age_seconds is None or freshness_age_seconds > max(0, int(max_age_seconds)):
            status = "stale_timestamp"
    # A derived market snapshot must never satisfy the real market-state gate.
    # Legacy tables do not have source_kind, so this only affects rows written
    # by the explicit fallback path.
    if rows and "source_kind" in columns:
        source_kind = con.execute(
            f'SELECT source_kind FROM "{relation}" WHERE CAST("{date_column}" AS VARCHAR)=?{as_of_filter} '
            f'ORDER BY {timestamp_column or date_column} DESC NULLS LAST LIMIT 1',
            base_params,
        ).fetchone()
        if source_kind and str(source_kind[0] or "").lower() in {
            "fallback",
            "derived",
            "derived_current",
        }:
            real_rows = 0
            status = "fallback_only"
    batch_meta = None
    sector_batch = None
    if relation == "multi_source_stock_flow" and table_exists(con, "intraday_stock_flow_batch"):
        try:
            batch_meta = con.execute(
                "SELECT expected_rows,fetched_rows,coverage_pct,status FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
        except Exception:
            batch_meta = None
        batch_status = str(batch_meta[3] or "") if batch_meta else ""
        batch_coverage = float(batch_meta[2] or 0) if batch_meta else 0.0
        batch_complete = (
            batch_status == "success"
            or (batch_status == "success_with_unavailable" and batch_coverage >= 99.5)
        )
        if batch_meta and (not batch_complete or int(batch_meta[0] or 0) <= 0
                           or batch_coverage < 80.0):
            status = "partial"
    if relation in {"multi_source_sector_flow", "sector_capital"} and table_exists(con, "intraday_sector_flow_batch"):
        try:
            sector_batch = con.execute(
                "SELECT expected_rows,fetched_rows,coverage_pct,status FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
        except Exception:
            sector_batch = None
        # The sector collector can be marked ``partial`` when an optional
        # taxonomy (for example TuShare DC) is unavailable even though the
        # required Eastmoney industry and THS concept universes are complete.
        # Coverage is the stronger gate for trading readiness; a 99.5%+
        # same-date snapshot is usable and remains visibly labelled partial.
        sector_complete = bool(
            sector_batch
            and int(sector_batch[0] or 0) > 0
            and float(sector_batch[2] or 0) >= 99.5
            and sector_batch[3] in {
                "success",
                "partial",
                "success_with_unavailable",
                "success_with_optional_gap",
            }
        )
        if sector_batch and not sector_complete:
            status = "partial"
    return {
        "relation": relation,
        "exists": True,
        "date_column": date_column,
        "rows": int(rows or 0),
        "real_rows": int(real_rows or 0),
        "fallback_rows": int(fallback_rows or 0),
        "latest_date": str(latest_date) if latest_date is not None else None,
        "latest_timestamp": str(latest_timestamp) if latest_timestamp is not None else None,
        "freshness_age_seconds": freshness_age_seconds,
        "max_age_seconds": int(max_age_seconds) if max_age_seconds is not None else None,
        "coverage_pct": float((batch_meta or sector_batch)[2]) if (batch_meta or locals().get("sector_batch")) else None,
        "expected_rows": int((batch_meta or sector_batch)[0]) if (batch_meta or locals().get("sector_batch")) else None,
        "status": status,
    }


def _sector_semantic_gate(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict:
    """Reject rows that are present but numerically unsafe for ranking."""
    if not table_exists(con, "multi_source_sector_flow"):
        # Older/minimal databases may legitimately expose only the normalized
        # legacy sector_capital table.  Keep that path usable, but make the
        # absence visible to callers so production reports can distinguish
        # "validated" from "legacy semantic checks unavailable".
        return {"ready": True, "invalid_rows": 0, "issues": ["multi_source_sector_flow missing; legacy validation only"]}
    cols = set(table_columns(con, "multi_source_sector_flow"))
    amount_expr = 'amount_unit IS NULL OR amount_unit NOT IN (\'yuan\', \'yuan_from_100m_yuan\')' if 'amount_unit' in cols else 'FALSE'
    invalid_unit = int(con.execute(
        f"SELECT count(*) FROM multi_source_sector_flow WHERE source_date=CAST(? AS DATE) AND ({amount_expr})",
        [trade_date],
    ).fetchone()[0])
    # A-share sector daily net flow is measured in yuan; values above 1e12
    # are a strong signal that a 10,000x "万元 -> 元" conversion was applied
    # to the DC endpoint, whose fields are already yuan.
    absurd = int(con.execute(
        "SELECT count(*) FROM multi_source_sector_flow "
        "WHERE source_date=CAST(? AS DATE) AND main_net IS NOT NULL AND abs(main_net)>1e12",
        [trade_date],
    ).fetchone()[0])
    issues = []
    if invalid_unit:
        issues.append(f"{invalid_unit} rows have unknown amount_unit")
    if absurd:
        issues.append(f"{absurd} rows have implausible sector main_net (>1e12 yuan)")
    return {"ready": not issues, "invalid_rows": invalid_unit + absurd, "issues": issues}


def assess_trade_date_readiness(
    db_path: str | Path | duckdb.DuckDBPyConnection,
    trade_date: str,
    stage: str = "close",
    required_groups: Iterable[str] | None = None,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> dict:
    trade_date = _normalize_trade_date(trade_date)
    # Freeze one local reference time for the whole assessment.  Besides
    # avoiding tiny timestamp drift between relations, exposing this value in
    # the report prevents an old trade date from being mistaken for current
    # readiness when an operator reviews a weekend or historical audit.
    now = as_local_naive(now) or datetime.now()
    selected_groups = tuple(required_groups or STAGE_REQUIREMENTS.get(stage, STAGE_REQUIREMENTS["close"]))
    # Postmarket is a completed-session review.  Its evidence contract is
    # exact trade-date coverage plus a timestamp from the same local date;
    # applying the intraday two-hour TTL after the close incorrectly turns a
    # valid 17:30 snapshot into a blocker when the report is opened later.
    freshness_max_age_seconds = None if stage == "postmarket" else max_age_seconds
    owns_connection = not isinstance(db_path, duckdb.DuckDBPyConnection)
    con = duckdb.connect(str(db_path), read_only=True) if owns_connection else db_path
    try:
        group_results = []
        for group_name in selected_groups:
            definition = GROUPS[group_name]
            relations = [
                {
                    **relation_freshness(
                        con,
                        name,
                        trade_date,
                        max_age_seconds=freshness_max_age_seconds,
                        now=now,
                    ),
                    "evidence_trade_date": trade_date,
                }
                for name in definition.relations
            ]
            ready_relation = next(
                (
                    item
                    for item in relations
                    if item["status"] == "ready" and item.get("latest_timestamp")
                ),
                None,
            ) or next((item for item in relations if item["status"] == "ready"), None)
            previous_context_date = None
            if (
                group_name == "market_state"
                and stage in {"premarket", "auction"}
                and ready_relation is None
            ):
                previous_context_date = _previous_open_session(con, trade_date)
                if previous_context_date:
                    previous_relations = [
                        {
                            **relation_freshness(
                                con,
                                name,
                                previous_context_date,
                                # A prior-session close is context, not a
                                # same-minute quote.  Do not apply the current
                                # intraday age limit to its timestamp.
                                max_age_seconds=None,
                                now=now,
                            ),
                            "evidence_trade_date": previous_context_date,
                        }
                        for name in definition.relations
                    ]
                    previous_ready = next(
                        (
                            item
                            for item in previous_relations
                            if item["status"] == "ready" and item.get("latest_timestamp")
                        ),
                        None,
                    ) or next(
                        (item for item in previous_relations if item["status"] == "ready"),
                        None,
                    )
                    relations.extend(previous_relations)
                    if previous_ready is not None:
                        ready_relation = previous_ready
            fallback_relation = next((item for item in relations if item["rows"] > 0), None)
            ready = bool(ready_relation or (definition.allow_fallback and fallback_relation))
            status = (
                "previous_session_context"
                if ready_relation is not None
                and previous_context_date
                and ready_relation.get("evidence_trade_date") == previous_context_date
                else "ready"
                if ready_relation
                else "fallback"
                if ready
                else "missing_or_stale"
            )
            group_results.append(
                {
                    "group": group_name,
                    "ready": ready,
                    "status": status,
                    "selected_relation": (ready_relation or fallback_relation or {}).get("relation"),
                    "context_trade_date": (
                        ready_relation.get("evidence_trade_date")
                        if status == "previous_session_context" and ready_relation
                        else None
                    ),
                    "relations": relations,
                }
            )
            # A full-market batch checkpoint and a realtime candidate-pool
            # snapshot are stronger contracts than the bounded fallback rows.
            # If either exists for this date but is partial, do not let a
            # smaller relation make the stage look actionable.
            if group_name == "stock_capital_flow" and table_exists(con, "intraday_stock_flow_batch"):
                batch = con.execute(
                    "SELECT status,coverage_pct FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)",
                    [trade_date],
                ).fetchone()
                batch_status = str(batch[0] or "") if batch else ""
                batch_coverage = float(batch[1] or 0) if batch else 0.0
                # Live sessions often land 99.0–99.5% with a few suspended names
                # and status ``partial``.  That is still usable for analytics and
                # candidate evidence; only fail clearly incomplete runs.
                batch_complete = (
                    batch_status == "success"
                    or (
                        batch_status
                        in {
                            "success_with_unavailable",
                            "partial",
                            "success_with_optional_gap",
                        }
                        and batch_coverage >= 99.5
                    )
                )
                if batch and (not batch_complete or batch_coverage < 80.0):
                    group_results[-1]["ready"] = False
                    group_results[-1]["status"] = "partial"
            if group_name == "candidate_pool" and table_exists(con, "realtime_candidate_pool_snapshot"):
                pool = con.execute(
                    "SELECT status,stock_count FROM realtime_candidate_pool_snapshot WHERE trade_date=CAST(? AS DATE)",
                    [trade_date],
                ).fetchone()
                if pool and (pool[0] != "success" or int(pool[1] or 0) <= 0):
                    group_results[-1]["ready"] = False
                    group_results[-1]["status"] = "partial"
            if group_name == "sector_capital_flow" and table_exists(con, "intraday_sector_flow_batch"):
                try:
                    sector_batch = con.execute(
                        "SELECT status,coverage_pct FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)",
                        [trade_date],
                    ).fetchone()
                except Exception:
                    sector_batch = None
                sector_complete = bool(
                    sector_batch
                    and float(sector_batch[1] or 0) >= 99.5
                    and sector_batch[0] in {
                        "success",
                        "partial",
                        "success_with_unavailable",
                        "success_with_optional_gap",
                    }
                )
                if sector_batch and not sector_complete:
                    group_results[-1]["ready"] = False
                    group_results[-1]["status"] = "partial"
            if group_name == "sector_capital_flow":
                semantic = _sector_semantic_gate(con, trade_date)
                group_results[-1]["semantic"] = semantic
                if not semantic["ready"]:
                    group_results[-1]["ready"] = False
                    group_results[-1]["status"] = "invalid"
            if group_name == "kline" and table_exists(con, "tushare_stock_basic"):
                expected = int(con.execute(
                    "SELECT count(DISTINCT ts_code) FROM tushare_stock_basic WHERE ts_code IS NOT NULL"
                ).fetchone()[0] or 0)
                selected = group_results[-1].get("selected_relation")
                selected_rows = next(
                    (int(item.get("rows") or 0) for item in relations if item.get("relation") == selected),
                    0,
                )
                minimum = max(1000, int(expected * 0.99 + 0.9999)) if expected >= 1000 else 1
                if expected >= 1000 and selected_rows < minimum:
                    group_results[-1]["ready"] = False
                    group_results[-1]["status"] = "partial"
                    group_results[-1]["coverage_pct"] = round(selected_rows * 100.0 / expected, 4)
                    group_results[-1]["expected_rows"] = expected
                    group_results[-1]["observed_rows"] = selected_rows
        # A close review needs a completed daily OHLC snapshot, not merely an
        # intraday/fallback kline.  Minimal unit-test databases may not have the
        # TuShare staging table; production databases do, so a stale source is
        # explicitly exposed as a separate blocking group.
        if stage in {"close", "postmarket"} and table_exists(con, "tushare_daily"):
            close_source = relation_freshness(
                con,
                "tushare_daily",
                trade_date,
                max_age_seconds=freshness_max_age_seconds,
                now=now,
            )
            expected = int(con.execute(
                "SELECT count(DISTINCT ts_code) FROM tushare_stock_basic WHERE ts_code IS NOT NULL"
            ).fetchone()[0] or 0) if table_exists(con, "tushare_stock_basic") else 0
            certification = None
            if table_exists(con, "close_snapshot_certification"):
                certification = con.execute(
                    "SELECT status,expected_rows,observed_rows,distinct_codes,invalid_rows,coverage_pct,error_message "
                    "FROM close_snapshot_certification WHERE dataset='daily' AND trade_date=CAST(? AS DATE) "
                    "AND provider='tushare'",
                    [trade_date],
                ).fetchone()
            if certification:
                cert_status, cert_expected, cert_observed, cert_distinct, cert_invalid, cert_coverage, cert_error = certification
                close_source["certification"] = {
                    "status": cert_status,
                    "expected_rows": cert_expected,
                    "observed_rows": cert_observed,
                    "distinct_codes": cert_distinct,
                    "invalid_rows": cert_invalid,
                    "coverage_pct": cert_coverage,
                    "error_message": cert_error,
                }
                if str(cert_status) != "certified":
                    close_source["status"] = "uncertified"
            elif expected >= 1000:
                observed = int(close_source.get("rows") or 0)
                minimum = max(1000, int(expected * 0.99 + 0.9999))
                close_source["coverage_pct"] = round(observed * 100.0 / expected, 4)
                close_source["expected_rows"] = expected
                close_source["observed_rows"] = observed
                if observed < minimum:
                    close_source["status"] = "partial"
            group_results.append(
                {
                    "group": "close_source",
                    "ready": close_source.get("status") == "ready" and close_source.get("rows", 0) > 0
                    and (not certification or str(certification[0]) == "certified"),
                    "status": close_source.get("status"),
                    "selected_relation": "tushare_daily",
                    "context_trade_date": None,
                    "relations": [close_source],
                }
            )
    finally:
        if owns_connection:
            con.close()
    missing = [item["group"] for item in group_results if not item["ready"]]
    # Candidate counts must describe this phase only.  Mixing prior intraday
    # rows into close readiness made a review-only close look entry-ready.
    stage_signal = {
        "premarket": "premarket_pool",
        "auction": "auction_confirmation",
        "intraday": "intraday_strength",
        "close": "close_decision",
        "postmarket": "close_decision",
    }.get(stage)

    def _candidate_counts(connection: duckdb.DuckDBPyConnection) -> tuple[int, int, int, int]:
        if not table_exists(connection, "stock_candidate_stage_signal"):
            return 0, 0, 0, 0
        columns = set(table_columns(connection, "stock_candidate_stage_signal"))
        actionable_expr = (
            "sum(CASE WHEN coalesce(is_actionable,false) THEN 1 ELSE 0 END)"
            if "is_actionable" in columns
            else "0"
        )
        tradable_expr = (
            "sum(CASE WHEN coalesce(tradable,false) THEN 1 ELSE 0 END)"
            if "tradable" in columns
            else "0"
        )
        risk_expr = (
            "sum(CASE WHEN coalesce(risk_approved,false) THEN 1 ELSE 0 END)"
            if "risk_approved" in columns
            else "0"
        )
        executable_expr = (
            "sum(CASE WHEN coalesce(is_executable,false) THEN 1 ELSE 0 END)"
            if "is_executable" in columns
            else "0"
        )
        filters = ["CAST(trade_date AS VARCHAR)=?"]
        params: list[str] = [trade_date]
        if stage_signal and "stage" in columns:
            filters.append("stage=?")
            params.append(stage_signal)
        row = connection.execute(
            "SELECT "
            f"{actionable_expr}, {tradable_expr}, {risk_expr}, {executable_expr} "
            "FROM stock_candidate_stage_signal WHERE " + " AND ".join(filters),
            params,
        ).fetchone()
        return tuple(int((value or 0)) for value in (row or (0, 0, 0, 0)))

    if isinstance(db_path, duckdb.DuckDBPyConnection):
        candidate_counts = _candidate_counts(db_path)
    else:
        _con = duckdb.connect(str(db_path), read_only=True)
        try:
            candidate_counts = _candidate_counts(_con)
        finally:
            _con.close()
    (
        actionable_candidates,
        tradable_candidates,
        risk_approved_candidates,
        executable_candidates,
    ) = candidate_counts
    source_ready = not missing
    # Capital-flow certification is a hard operational input.  There is no
    # third "not assessed" ready state in the daily report: when the
    # independent reconciliation has not run, the result is explicitly false
    # and the operator state stays blocked/uncertified.
    pipeline_ready = source_ready
    artifact_current = True
    operator_state = build_operator_state(
        source_ready=source_ready,
        pipeline_ready=pipeline_ready,
        artifact_current=artifact_current,
        data_certified_ready=source_ready and pipeline_ready and artifact_current,
        flow_certified_ready=False,
        execution_ready=executable_candidates > 0,
        run_status="not_run",
        blockers=missing,
    )
    return {
        **operator_state,
        "operator_state": dict(operator_state),
        "trade_date": trade_date,
        "stage": stage,
        "as_of": now.isoformat(sep=" ", timespec="seconds"),
        "max_age_seconds": int(max_age_seconds) if max_age_seconds is not None else None,
        "freshness_contract": (
            "same_trade_date_after_close"
            if stage == "postmarket" else "timestamp_ttl"
        ),
        "actionable_candidates": actionable_candidates,
        "tradable_candidates": tradable_candidates,
        "risk_approved_candidates": risk_approved_candidates,
        "executable_candidates": executable_candidates,
        "missing_groups": missing,
        "groups": group_results,
    }


def render_readiness_markdown(result: dict) -> str:
    analytics_ready = result.get("analytics_ready", result["ready"])
    execution_ready = result.get("execution_ready", False)
    actionable_candidates = result.get("actionable_candidates", 0)
    tradable_candidates = result.get("tradable_candidates", 0)
    risk_approved_candidates = result.get("risk_approved_candidates", 0)
    executable_candidates = result.get("executable_candidates", 0)
    lines = [
        "# Trade Date Readiness",
        "",
        f"- Trade date: `{result['trade_date']}`",
        f"- Stage: `{result['stage']}`",
        f"- As of: `{result.get('as_of') or 'runtime clock'}`",
        f"- Source ready: `{str(result.get('source_ready', analytics_ready)).lower()}`",
        f"- Pipeline ready: `{str(result.get('pipeline_ready', analytics_ready)).lower()}`",
        f"- Artifact current: `{str(result.get('artifact_current', False)).lower()}`",
        f"- Data certified ready: `{str(result.get('data_certified_ready', False)).lower()}`",
        f"- Flow certified ready: `{str(result.get('flow_certified_ready', 'not_assessed')).lower()}`",
        f"- Analysis ready: `{str(result.get('analysis_ready', analytics_ready)).lower()}`",
        f"- Execution ready: `{str(execution_ready).lower()}`",
        f"- Actionable candidates: `{actionable_candidates}`",
        f"- Tradable candidates: `{tradable_candidates}`",
        f"- Risk-approved candidates: `{risk_approved_candidates}`",
        f"- Executable candidates: `{executable_candidates}`",
        f"- Missing groups: `{', '.join(result['missing_groups']) or 'none'}`",
    ]
    if analytics_ready and not execution_ready:
        lines.append(
            "- WARNING: data is present but no candidate is entry-executable "
            "(e.g. delayed providers or close signal_close review-only); "
            "treat as analysis-only, NOT entry-ready."
        )
    lines.extend([
        "",
        "| Group | Status | Context date | Selected relation | Rows | Latest date | Latest timestamp |",
        "|---|---|---|---|---:|---|---|",
    ])
    for group in result["groups"]:
        selected = next(
            (item for item in group["relations"] if item["relation"] == group["selected_relation"]),
            {},
        )
        lines.append(
            f"| {group['group']} | {group['status']} | {group.get('context_trade_date') or ''} | "
            f"{group['selected_relation'] or ''} | "
            f"{selected.get('rows', 0)} | {selected.get('latest_date') or ''} | "
            f"{selected.get('latest_timestamp') or ''} |"
        )
        semantic = group.get("semantic")
        if semantic and semantic.get("issues"):
            lines.append(f"- `{group['group']}` semantic issues: {'; '.join(semantic['issues'])}")
    lines.append("")
    return "\n".join(lines)
