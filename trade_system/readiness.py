"""Trade-date freshness and actionability gates.

The project used to treat a relation as available when it contained any row
from any date.  Trading decisions need a stricter contract: required evidence
must exist for the requested trade date (or the immediately preceding session
for pre-market context), and fallback-only evidence must remain visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
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


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _coverage_fact(expected, observed, coverage, status, good_statuses) -> dict:
    known = _finite_number(expected) and expected > 0 and float(expected).is_integer()
    counts_valid = bool(known and _finite_number(observed) and 0 <= observed <= expected
                        and float(observed).is_integer())
    percentage_valid = _finite_number(coverage) and 0 <= coverage <= 100
    actual_pct = observed * 100.0 / expected if counts_valid else None
    passed = bool(counts_valid and percentage_valid and coverage >= 99.5
                  and actual_pct >= 99.5 and coverage <= actual_pct + 0.011
                  and status in good_statuses)
    return {
        "passed": passed, "denominator_known": bool(known),
        "expected_rows": int(expected) if known else None,
        "fetched_rows": int(observed) if _finite_number(observed) and float(observed).is_integer() else None,
        "coverage_pct": float(coverage) if known and percentage_valid else None,
        "observed_coverage_pct": actual_pct, "status": str(status or "missing"),
        "reason": None if passed else "invalid_or_incomplete_coverage_evidence",
    }


def required_sector_taxonomy_coverage(con, trade_date, *, now=None):
    """Require each dated product's own valid denominator, not aggregate counts."""
    required = ("em_industry", "ths_concept")
    table = "intraday_sector_flow_taxonomy"
    if not table_exists(con, table):
        return {"passed": False, "taxonomies": {}, "reason": "required_taxonomy_evidence_missing"}
    timestamp = _timestamp_column(con, table)
    time_sql = f', TRY_CAST("{timestamp}" AS TIMESTAMP)' if timestamp else ', NULL'
    try:
        rows = con.execute(
            "SELECT taxonomy,expected_rows,fetched_rows,coverage_pct,status" + time_sql
            + " FROM intraday_sector_flow_taxonomy WHERE trade_date=CAST(? AS DATE)",
            [_normalize_trade_date(trade_date)],
        ).fetchall()
    except duckdb.Error:
        return {"passed": False, "taxonomies": {}, "reason": "required_taxonomy_evidence_invalid"}
    facts = {}
    for name, expected, observed, coverage, status, updated_at in rows:
        if name not in required:
            continue
        if name in facts:
            return {"passed": False, "taxonomies": facts, "reason": "duplicate_taxonomy_evidence"}
        fact = _coverage_fact(expected, observed, coverage, status, {"success"})
        if now is not None and timestamp and (updated_at is None or updated_at > as_local_naive(now)):
            fact.update(passed=False, reason="taxonomy_evidence_not_available_as_of")
        facts[name] = fact
    passed = all(name in facts and facts[name]["passed"] for name in required)
    return {"passed": passed, "taxonomies": facts,
            "reason": None if passed else "required_taxonomy_missing_unknown_or_partial"}


def _actual_flow_coverage(con, trade_date, kind, evidence, *, max_age_seconds=None, now=None):
    """Compare a checkpoint with usable canonical facts, never a bounded fallback."""
    relation = "multi_source_stock_flow" if kind == "stock" else "multi_source_sector_flow"
    result = {"passed": False, "relation": relation, "reason": "canonical_flow_evidence_missing"}
    if not table_exists(con, relation):
        return result
    columns = set(table_columns(con, relation))
    code = "stock_code" if kind == "stock" else "sector_code"
    if not {"source_date", code, "main_net"} <= columns:
        return {**result, "reason": "canonical_flow_schema_invalid"}
    filters = ['source_date=CAST(? AS DATE)', 'isfinite(TRY_CAST(main_net AS DOUBLE))',
               f'nullif(trim("{code}"),\'\') IS NOT NULL']
    params = [_normalize_trade_date(trade_date)]
    if "is_stale" in columns:
        filters.append("coalesce(is_stale,false)=false")
    if "is_fallback" in columns:
        filters.append("coalesce(is_fallback,false)=false")
    timestamp = _timestamp_column(con, relation)
    if timestamp:
        time_expr = f'TRY_CAST("{timestamp}" AS TIMESTAMP)'
        filters.append(f"{time_expr} IS NOT NULL")
        if now is not None:
            filters.append(f"{time_expr}<=?")
            params.append(as_local_naive(now))
        if max_age_seconds is not None:
            filters.append(f"{time_expr}>=?")
            params.append((as_local_naive(now) or datetime.now())
                          - timedelta(seconds=max(0, int(max_age_seconds))))
    else:
        return {**result, "reason": "canonical_flow_timestamp_missing"}
    if kind == "stock":
        provider = evidence.get("provider")
        if provider:
            if "provider" not in columns:
                return {**result, "reason": "canonical_flow_provider_missing"}
            filters.append("provider=?")
            params.append(provider)
        rows = con.execute(f'SELECT count(DISTINCT "{code}") FROM "{relation}" WHERE '
                           + " AND ".join(filters), params).fetchone()[0]
        expected = evidence.get("expected_rows")
        pct = rows * 100.0 / expected if expected else None
        passed = bool(expected and 99.5 <= pct <= 100)
        return {**result, "passed": passed, "observed_codes": rows, "coverage_pct": pct,
                "reason": None if passed else "canonical_flow_scope_incomplete"}
    if "sector_type" not in columns:
        return {**result, "reason": "canonical_taxonomy_identity_missing"}
    actual = {}
    for name, fact in evidence.get("taxonomies", {}).items():
        types = ("ths_concept", "ths_concept_derived") if name == "ths_concept" else (name,)
        rows = con.execute(f'SELECT count(DISTINCT "{code}") FROM "{relation}" WHERE '
                           + " AND ".join(filters) + " AND sector_type IN ("
                           + ",".join("?" for _ in types) + ")", params + list(types)).fetchone()[0]
        expected = fact.get("expected_rows")
        pct = rows * 100.0 / expected if expected else None
        actual[name] = {"observed_codes": rows, "coverage_pct": pct,
                        "passed": bool(expected and 99.5 <= pct <= 100)}
    passed = all(name in actual and actual[name]["passed"] for name in ("em_industry", "ths_concept"))
    return {**result, "passed": passed, "taxonomies": actual,
            "reason": None if passed else "canonical_taxonomy_scope_incomplete"}


def capital_flow_coverage(con, trade_date, kind, *, require_actual=False, max_age_seconds=None, now=None):
    """Single gate shared by relation and group: missing metadata is unknown."""
    if kind not in {"stock", "sector"}:
        raise ValueError("unknown capital flow kind")
    table = f"intraday_{kind}_flow_batch"
    result = {"passed": False, "denominator_known": False, "expected_rows": None,
              "fetched_rows": None, "coverage_pct": None, "status": "missing",
              "reason": "batch_evidence_missing"}
    if not table_exists(con, table):
        return result
    columns = set(table_columns(con, table))
    timestamp = _timestamp_column(con, table)
    time_sql = f', TRY_CAST("{timestamp}" AS TIMESTAMP)' if timestamp else ', NULL'
    provider_sql = ', provider' if "provider" in columns else ', NULL'
    try:
        rows = con.execute("SELECT expected_rows,fetched_rows,coverage_pct,status" + time_sql + provider_sql
                           + f" FROM {table} WHERE trade_date=CAST(? AS DATE)",
                           [_normalize_trade_date(trade_date)]).fetchall()
    except duckdb.Error:
        return {**result, "reason": "batch_evidence_invalid"}
    if len(rows) != 1:
        return {**result, "reason": "duplicate_batch_evidence" if rows else "same_date_batch_evidence_missing"}
    expected, observed, pct, status, updated_at, provider = rows[0]
    good = {"success", "success_with_unavailable"} if kind == "stock" else {"success", "success_with_optional_gap"}
    result = _coverage_fact(expected, observed, pct, status, good)
    result.update(updated_at=str(updated_at) if updated_at else None, provider=provider)
    if now is not None and timestamp and (updated_at is None or updated_at > as_local_naive(now)):
        result.update(passed=False, reason="batch_evidence_not_available_as_of")
    if kind == "sector":
        required = required_sector_taxonomy_coverage(con, trade_date, now=now)
        result["taxonomies"] = required["taxonomies"]
        if not required["passed"]:
            result.update(passed=False, reason=required["reason"])
    if require_actual:
        actual = _actual_flow_coverage(con, trade_date, kind, result,
                                       max_age_seconds=max_age_seconds, now=now)
        result["actual"] = actual
        if not actual["passed"]:
            result["passed"] = False
            if result.get("reason") is None:
                result["reason"] = actual["reason"]
    return result


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
    trade_date = _normalize_trade_date(trade_date)
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
    # In the production database v_kline_daily is a canonical/fallback view
    # over the large TuShare staging table.  Re-expanding its de-duplication
    # window for every readiness relation can consume several GB and is
    # unnecessary for the close gate, which separately certifies tushare_daily.
    # Read the same authority directly while keeping the public relation name
    # in the readiness payload.
    if relation == "v_kline_daily" and table_exists(con, "tushare_daily"):
        result = relation_freshness(
            con,
            "tushare_daily",
            trade_date,
            max_age_seconds=max_age_seconds,
            now=now,
        )
        result["relation"] = relation
        result["source_relation"] = "tushare_daily"
        return result
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
    # Legacy relations may store fetched_at/updated_at as VARCHAR while newer
    # relations use TIMESTAMP.  Cast at the SQL boundary so both schemas obey
    # the same as-of and freshness contract.
    timestamp_value_expr = (
        f'TRY_CAST("{timestamp_column}" AS TIMESTAMP)'
        if timestamp_column else "NULL"
    )
    timestamp_expr = f'max({timestamp_value_expr})' if timestamp_column else "NULL"
    flow_columns = (
        ("main_net", "super_net", "large_net", "mid_net", "small_net")
        if relation == "multi_source_sector_flow"
        else ("main_net_inflow", "super_net_inflow", "big_net_inflow", "mid_net_inflow", "small_net_inflow")
        if relation in {"sector_capital", "v_sector_capital"}
        else ()
    )
    valid_flow = (
        "(" + " OR ".join(f'isfinite(TRY_CAST("{name}" AS DOUBLE))' for name in flow_columns if name in columns) + ")"
        if any(name in columns for name in flow_columns)
        else "TRUE"
    )
    # A stock-flow row with only bucket placeholders is not usable evidence.
    # Count only rows with a canonical main-order value; the raw total-net
    # value remains available through ``net_total`` for separate analysis.
    if relation == "multi_source_stock_flow" and "main_net" in columns:
        valid_flow = 'isfinite(TRY_CAST("main_net" AS DOUBLE))'
    if "is_stale" in columns:
        valid_flow += " AND coalesce(is_stale,false)=false"
    as_of_filter = (
        f" AND {timestamp_value_expr} <= ?"
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
    same_date_rows = int(rows or 0)
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
                  AND {timestamp_value_expr} >= ?
                  AND {timestamp_value_expr} <= ?
                """,
                [trade_date, cutoff, now or datetime.now()],
            ).fetchone()
        except (TypeError, ValueError):
            pass
    status = "ready" if rows and real_rows else "fallback_only" if rows else "stale_or_empty"
    freshness_age_seconds = None
    if timestamp_column and latest_timestamp is not None:
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
    data_status = status
    kind = (
        "stock" if relation in GROUPS["stock_capital_flow"].relations
        else "sector" if relation in {*GROUPS["sector_capital_flow"].relations, "multi_source_sector_flow"}
        else None
    )
    coverage = None
    if kind:
        coverage = capital_flow_coverage(
            con, trade_date, kind, require_actual=True, max_age_seconds=max_age_seconds, now=now,
        )
        # Qualification and freshness are independent facts.  An unknown
        # denominator must block readiness without concealing a stale source.
        if not coverage["passed"] and status == "ready":
            status = "partial"
    return {
        "relation": relation,
        "exists": True,
        "date_column": date_column,
        "rows": int(rows or 0),
        "same_date_rows": same_date_rows,
        "real_rows": int(real_rows or 0),
        "fallback_rows": int(fallback_rows or 0),
        "latest_date": str(latest_date) if latest_date is not None else None,
        "latest_timestamp": str(latest_timestamp) if latest_timestamp is not None else None,
        "freshness_age_seconds": freshness_age_seconds,
        "max_age_seconds": int(max_age_seconds) if max_age_seconds is not None else None,
        "coverage_pct": coverage.get("coverage_pct") if coverage else None,
        "expected_rows": coverage.get("expected_rows") if coverage else None,
        "coverage": coverage,
        "coverage_reason": coverage.get("reason") if coverage else None,
        "data_status": data_status,
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
    if not {"source_date", "main_net"} <= cols:
        return {"ready": False, "invalid_rows": 0,
                "issues": ["canonical sector semantic evidence schema invalid"]}
    amount_expr = (
        "amount_unit IS NULL OR amount_unit NOT IN "
        "('yuan', 'yuan_from_10000', 'yuan_from_100m_yuan')"
        if 'amount_unit' in cols else 'FALSE'
    )
    invalid_unit = int(con.execute(
        f"SELECT count(*) FROM multi_source_sector_flow WHERE source_date=CAST(? AS DATE) AND ({amount_expr})",
        [trade_date],
    ).fetchone()[0])
    # A-share sector daily net flow is measured in yuan; values above 1e12
    # are a strong signal that a 10,000x "万元 -> 元" conversion was applied
    # to the DC endpoint, whose fields are already yuan.
    absurd = int(con.execute(
        "SELECT count(*) FROM multi_source_sector_flow "
        "WHERE source_date=CAST(? AS DATE) AND main_net IS NOT NULL "
        "AND (NOT isfinite(TRY_CAST(main_net AS DOUBLE)) OR abs(TRY_CAST(main_net AS DOUBLE))>1e12)",
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
    # Close/postmarket replays are historical evidence reviews.  Once the
    # requested session is before the local audit date, the exact trade-date
    # row and its certification are authoritative; applying today's TTL to a
    # yesterday's 17:30 snapshot creates false market/flow blockers.  A same-
    # day close still uses the TTL so a stale intraday snapshot cannot pass.
    historical_close = (
        stage in {"close", "postmarket"}
        and date.fromisoformat(trade_date) < now.date()
    )
    freshness_max_age_seconds = (
        None if stage == "postmarket" or historical_close else max_age_seconds
    )
    if stage == "close" and now.date() == date.fromisoformat(trade_date):
        boundary = datetime.fromisoformat(trade_date + "T15:00:00")
        if now >= boundary:
            freshness_max_age_seconds = int((now - boundary).total_seconds())
    owns_connection = not isinstance(db_path, duckdb.DuckDBPyConnection)
    con = duckdb.connect(str(db_path), read_only=True) if owns_connection else db_path
    try:
        group_results = []
        for group_name in selected_groups:
            definition = GROUPS[group_name]
            capital_coverage = None
            if group_name in {"stock_capital_flow", "sector_capital_flow"}:
                capital_coverage = capital_flow_coverage(
                    con, trade_date, "stock" if group_name == "stock_capital_flow" else "sector",
                    require_actual=True, max_age_seconds=freshness_max_age_seconds, now=now,
                )
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
            # Missing metadata is unknown, even when a view or bounded feed
            # contains a current row.  Apply the same gate unconditionally to
            # every candidate relation, then preserve both reasons in reports.
            if capital_coverage is not None:
                group_results[-1]["coverage"] = capital_coverage
                group_results[-1]["coverage_reason"] = capital_coverage.get("reason")
                if not capital_coverage["passed"]:
                    group_results[-1]["ready"] = False
                    group_results[-1]["data_status"] = (
                        "ready" if any(item.get("data_status") == "ready" for item in relations)
                        else status
                    )
                    if group_results[-1]["data_status"] == "ready":
                        group_results[-1]["status"] = "partial"
            if group_name == "candidate_pool" and table_exists(con, "realtime_candidate_pool_snapshot"):
                pool = con.execute(
                    "SELECT status,stock_count FROM realtime_candidate_pool_snapshot WHERE trade_date=CAST(? AS DATE)",
                    [trade_date],
                ).fetchone()
                if pool and (pool[0] != "success" or int(pool[1] or 0) <= 0):
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
                    "SELECT count(DISTINCT ts_code) FROM tushare_stock_basic WHERE ts_code IS NOT NULL "
                    "AND (list_date IS NULL OR list_date<=CAST(? AS DATE)) "
                    "AND (delist_date IS NULL OR delist_date>CAST(? AS DATE))", [trade_date, trade_date]
                ).fetchone()[0] or 0)
                if table_exists(con, 'close_snapshot_certification'):
                    certified = con.execute("SELECT expected_rows,distinct_codes FROM close_snapshot_certification "
                        "WHERE dataset='daily' AND trade_date=CAST(? AS DATE) AND status='certified' "
                        "AND invalid_rows=0 AND coverage_pct>=99.5 ORDER BY fetched_at DESC LIMIT 1", [trade_date]).fetchone()
                    if certified and certified[0] >= 1000 and certified[1] >= certified[0]:
                        expected = int(certified[0])
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
                    "SELECT status,expected_rows,observed_rows,distinct_codes,invalid_rows,coverage_pct,error_message,provider "
                    "FROM close_snapshot_certification WHERE dataset='daily' AND trade_date=CAST(? AS DATE) "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    [trade_date],
                ).fetchone()
            if certification:
                cert_status, cert_expected, cert_observed, cert_distinct, cert_invalid, cert_coverage, cert_error, cert_provider = certification
                close_source["certification"] = {
                    "status": cert_status,
                    "expected_rows": cert_expected,
                    "observed_rows": cert_observed,
                    "distinct_codes": cert_distinct,
                    "invalid_rows": cert_invalid,
                    "coverage_pct": cert_coverage,
                    "error_message": cert_error,
                    "provider": cert_provider,
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
    # Capital-flow certification is a hard operational input and has one
    # authority: ``assess_capital_flow_health``.  Hard-coding False here made
    # the readiness report contradict the capital-flow report produced a few
    # seconds earlier in the same run.
    try:
        from trade_system.capital_flow_health import assess_capital_flow_health

        flow_db_path = db_path
        if isinstance(db_path, duckdb.DuckDBPyConnection):
            db_rows = db_path.execute("PRAGMA database_list").fetchall()
            persisted = next(
                (str(row[2]) for row in db_rows if len(row) > 2 and row[2]),
                "",
            )
            if not persisted:
                raise ValueError("capital flow certification requires a persisted database")
            flow_db_path = persisted
        flow_health = assess_capital_flow_health(
            flow_db_path,
            trade_date,
            max_age_seconds=max_age_seconds,
            now=now,
            session_close=stage in {"close", "postmarket"},
        )
        flow_certified_ready = bool(flow_health.get("flow_certified_ready", False))
        flow_blockers = list(flow_health.get("blockers") or [])
        flow_warnings = list(flow_health.get("warnings") or [])
    except Exception as exc:
        flow_health = {
            "flow_certified_ready": False,
            "blockers": [f"capital_flow_health_error:{type(exc).__name__}"],
        }
        flow_certified_ready = False
        flow_blockers = list(flow_health["blockers"])
        flow_warnings = []
    pipeline_ready = source_ready
    artifact_current = True
    operator_state = build_operator_state(
        source_ready=source_ready,
        pipeline_ready=pipeline_ready,
        artifact_current=artifact_current,
        data_certified_ready=source_ready and pipeline_ready and artifact_current,
        flow_certified_ready=flow_certified_ready,
        execution_ready=executable_candidates > 0,
        # This function evaluates data state only.  It must not claim that a
        # review or pipeline run was published; publication provenance belongs
        # to the pipeline manifest.
        run_status="not_published",
        blockers=missing + flow_blockers,
        warnings=flow_warnings,
    )
    # Keep the raw stage-signal count for diagnostics, but expose only the
    # effective count under the operator-facing name.  Otherwise a row left
    # executable by an earlier stage can coexist with execution_ready=false
    # and make the readiness report contradict itself.
    effective_executable_candidates = (
        executable_candidates if operator_state["execution_ready"] else 0
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
            if stage == "postmarket" or historical_close else "timestamp_ttl"
        ),
        "actionable_candidates": actionable_candidates,
        "tradable_candidates": tradable_candidates,
        "risk_approved_candidates": risk_approved_candidates,
        "executable_candidates": effective_executable_candidates,
        "candidate_pool_executable_candidates": executable_candidates,
        "missing_groups": missing,
        "groups": group_results,
        "capital_flow_health": flow_health,
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
