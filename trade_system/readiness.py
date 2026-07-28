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
    rows, real_rows, fallback_rows, latest_timestamp = con.execute(
        f"""
        SELECT count(*), {real_expr}, {fallback_expr}, {timestamp_expr}
        FROM "{relation}"
        WHERE CAST("{date_column}" AS VARCHAR) = ? AND {valid_flow}
        """,
        [trade_date],
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
                observed_at = datetime.fromisoformat(observed_at)
            cutoff = (now or datetime.now()) - timedelta(seconds=max(0, int(max_age_seconds)))
            rows, real_rows, fallback_rows = con.execute(
                f"""
                SELECT count(*), {real_expr}, {fallback_expr}
                FROM "{relation}"
                WHERE CAST("{date_column}" AS VARCHAR) = ?
                  AND {valid_flow}
                  AND "{timestamp_column}" >= ?
                """,
                [trade_date, cutoff],
            ).fetchone()
        except (TypeError, ValueError):
            pass
    status = "ready" if rows and real_rows else "fallback_only" if rows else "stale_or_empty"
    freshness_age_seconds = None
    if rows and timestamp_column and latest_timestamp is not None:
        try:
            observed_at = latest_timestamp
            if isinstance(observed_at, str):
                observed_at = datetime.fromisoformat(observed_at)
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
            f'SELECT source_kind FROM "{relation}" WHERE CAST("{date_column}" AS VARCHAR)=? '
            f'ORDER BY {timestamp_column or date_column} DESC NULLS LAST LIMIT 1',
            [trade_date],
        ).fetchone()
        if source_kind and str(source_kind[0] or "").lower() == "fallback":
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
    selected_groups = tuple(required_groups or STAGE_REQUIREMENTS.get(stage, STAGE_REQUIREMENTS["close"]))
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
                        max_age_seconds=max_age_seconds,
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
                batch_complete = (
                    batch_status == "success"
                    or (batch_status == "success_with_unavailable" and batch_coverage >= 99.5)
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
    finally:
        if owns_connection:
            con.close()
    missing = [item["group"] for item in group_results if not item["ready"]]
    # P0#3: distinguish data-readiness (analytics) from execution-readiness.  Data can
    # be fully present yet only from a delayed provider that cannot back a real order;
    # operators must not see a green "actionable" signal in that case.  Count the
    # executable candidates already persisted for this date on an independent
    # read-only connection (the assessment connection is closed in the finally above).
    actionable_candidates = 0
    try:
        if isinstance(db_path, duckdb.DuckDBPyConnection):
            actionable_candidates = int(db_path.execute(
                "SELECT count(*) FROM stock_candidate_stage_signal "
                "WHERE CAST(trade_date AS VARCHAR)=? AND coalesce(is_actionable,false)=true",
                [trade_date],
            ).fetchone()[0] or 0)
        else:
            _con = duckdb.connect(str(db_path), read_only=True)
            try:
                actionable_candidates = int(_con.execute(
                    "SELECT count(*) FROM stock_candidate_stage_signal "
                    "WHERE CAST(trade_date AS VARCHAR)=? AND coalesce(is_actionable,false)=true",
                    [trade_date],
                ).fetchone()[0] or 0)
            finally:
                _con.close()
    except Exception:
        actionable_candidates = 0
    analytics_ready = not missing
    return {
        "trade_date": trade_date,
        "stage": stage,
        "max_age_seconds": int(max_age_seconds) if max_age_seconds is not None else None,
        # ``ready`` stays the data-readiness gate (drives pipeline exit codes); the
        # analytics/execution split below is the operator-facing clarification.
        "ready": analytics_ready,
        "analytics_ready": analytics_ready,
        "execution_ready": actionable_candidates > 0,
        "actionable_candidates": actionable_candidates,
        "missing_groups": missing,
        "groups": group_results,
    }


def render_readiness_markdown(result: dict) -> str:
    analytics_ready = result.get("analytics_ready", result["ready"])
    execution_ready = result.get("execution_ready", False)
    actionable_candidates = result.get("actionable_candidates", 0)
    lines = [
        "# Trade Date Readiness",
        "",
        f"- Trade date: `{result['trade_date']}`",
        f"- Stage: `{result['stage']}`",
        f"- Analytics ready: `{str(analytics_ready).lower()}`",
        f"- Execution ready: `{str(execution_ready).lower()}`",
        f"- Actionable candidates: `{actionable_candidates}`",
        f"- Missing groups: `{', '.join(result['missing_groups']) or 'none'}`",
    ]
    if analytics_ready and not execution_ready:
        lines.append(
            "- WARNING: data is present but no candidate is executable "
            "(e.g. only delayed providers); treat as analysis-only, NOT execution-ready."
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
