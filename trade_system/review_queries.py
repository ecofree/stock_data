"""Read-only review context builders (query layer).

Every function here takes a DuckDB connection plus a trade date and
returns plain dicts; no rendering, no file IO.  ``daily_review``
re-exports these names for backward compatibility.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb

from trade_system.db_utils import fetch_dicts as _fetch_dicts
from trade_system.logging_setup import get_logger
from trade_system.quality import table_columns, table_exists

logger = get_logger(__name__)


def _rows(con: duckdb.DuckDBPyConnection, table: str, sql: str, params: list[Any]) -> list[dict]:
    if not table_exists(con, table):
        return []
    try:
        return _fetch_dicts(con, sql, params)
    except Exception as exc:
        logger.warning("review query failed for table %s: %s", table, exc)
        return []


def _latest_date(con: duckdb.DuckDBPyConnection) -> str:
    for table in ("market_regime_snapshot", "stock_candidate_stage_signal", "stock_candidate_score"):
        if table_exists(con, table):
            row = con.execute(f"SELECT max(trade_date) FROM {table}").fetchone()
            if row and row[0]:
                return str(row[0])
    return ""


def _capital_flow_review(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """Build the explicit daily fund-flow review contract.

    Rows are deduplicated by asset code before ranking.  Sector rankings are
    kept in one taxonomy (THS concepts when available, otherwise the clearly
    labelled derived concept fallback) instead of mixing DC industries and
    concept aggregates with incompatible semantics.
    """
    result: dict[str, Any] = {
        "stock_inflow": [], "stock_outflow": [], "sector_inflow": [],
        "sector_outflow": [], "industry_inflow": [], "industry_outflow": [],
        "sector_limit_up": [], "stock_flow_meta": {},
        "sector_flow_meta": {}, "candidate_picks": [],
        "stock_flow_persistence": [], "stock_flow_persistence_outflow": [],
        "sector_flow_persistence": [], "sector_flow_persistence_outflow": [],
        "lhb": [], "coverage_alerts": [],
    }
    if table_exists(con, "multi_source_stock_flow"):
        result["stock_flow_meta"] = _fetch_dicts(
            con,
            """
            SELECT count(*) AS rows, count(DISTINCT stock_code) AS codes,
                   max(fetched_at) AS fetched_at,
                   coalesce((SELECT status FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)), 'unknown') AS batch_status,
                   coalesce((SELECT coverage_pct FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)), NULL) AS batch_coverage_pct,
                   string_agg(DISTINCT coalesce(provider, 'unknown'), ', ' ORDER BY coalesce(provider, 'unknown')) AS providers
            FROM multi_source_stock_flow
            WHERE source_date=CAST(? AS DATE) AND coalesce(is_stale,FALSE)=FALSE
            """,
            [trade_date, trade_date, trade_date],
        )[0]
        member_name_join = ""
        member_name_expr = "NULL"
        if table_exists(con, "v_default_concept_stock_history"):
            # Tushare stock_basic can lag newly listed names.  The quality-
            # gated THS membership snapshot is a same-date, already-used
            # catalogue; prefer an unprefixed name (rather than N/C listing
            # markers) when it is available.
            member_name_join = """
                LEFT JOIN (
                    SELECT stock_code, stock_name
                    FROM (
                        SELECT stock_code, stock_name,
                               row_number() OVER (
                                   PARTITION BY stock_code
                                   ORDER BY CASE
                                       WHEN left(coalesce(stock_name, ''), 1) IN ('N', 'C') THEN 1
                                       ELSE 0
                                   END,
                                   length(coalesce(stock_name, '')),
                                   stock_name
                               ) AS name_rank
                        FROM v_default_concept_stock_history
                        WHERE trade_date=(
                            SELECT max(trade_date)
                            FROM v_default_concept_stock_history
                            WHERE trade_date<=CAST(? AS DATE)
                        )
                          AND stock_name IS NOT NULL
                    ) names
                    WHERE name_rank=1
                ) m ON m.stock_code=f.stock_code
            """
            member_name_expr = "nullif(m.stock_name, '')"
        stock_sql = f"""
            WITH ranked AS (
                SELECT f.*, coalesce(
                           nullif(json_extract_string(f.raw_json, '$.name'), ''),
                           nullif(b.stock_name, ''),
                           {member_name_expr},
                           f.stock_code
                       ) AS stock_name,
                       row_number() OVER (
                         PARTITION BY f.stock_code
                          ORDER BY CASE f.provider
                                     WHEN 'eastmoney_market' THEN 1
                                     WHEN 'eastmoney_intraday_clist_delay' THEN 2
                                     WHEN 'eastmoney_intraday_clist' THEN 3
                                     WHEN 'tushare' THEN 4
                                     WHEN 'tushare_relay' THEN 4
                                     WHEN 'kpl' THEN 5
                                     ELSE 9
                                   END,
                                  f.fetched_at DESC NULLS LAST
                       ) AS provider_rank
                FROM multi_source_stock_flow f
                LEFT JOIN tushare_stock_basic b ON b.stock_code=f.stock_code
                {member_name_join}
                WHERE f.source_date=CAST(? AS DATE) AND coalesce(f.is_stale,FALSE)=FALSE
            ), deduped AS (SELECT * FROM ranked WHERE provider_rank=1)
            SELECT stock_code, stock_name, main_net, super_net, large_net, close,
                   change_pct, turnover, provider, fetched_at
            FROM deduped ORDER BY main_net {{direction}} NULLS LAST LIMIT 50
        """
        try:
            query_params = [trade_date, trade_date] if member_name_join else [trade_date]
            result["stock_inflow"] = _fetch_dicts(con, stock_sql.format(direction="DESC"), query_params)
            result["stock_outflow"] = _fetch_dicts(con, stock_sql.format(direction="ASC"), query_params)
        except Exception:
            pass

    if table_exists(con, "multi_source_sector_flow"):
        result["sector_flow_meta"] = _fetch_dicts(
            con,
            """
            SELECT count(*) AS rows, count(DISTINCT sector_code) AS codes,
                   max(fetched_at) AS fetched_at,
                   coalesce((SELECT status FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)), 'unknown') AS batch_status,
                   coalesce((SELECT coverage_pct FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)), NULL) AS batch_coverage_pct,
                   string_agg(DISTINCT coalesce(sector_type,'unknown'), ', ' ORDER BY coalesce(sector_type,'unknown')) AS taxonomy
            FROM multi_source_sector_flow
            WHERE source_date=CAST(? AS DATE) AND coalesce(is_stale,FALSE)=FALSE
            """,
            [trade_date, trade_date, trade_date],
        )[0]
        sector_sql = """
            SELECT sector_code, sector_name, sector_type, main_net, change_pct,
                   provider, fetched_at
            FROM multi_source_sector_flow
            WHERE source_date=CAST(? AS DATE)
              AND coalesce(is_stale,FALSE)=FALSE
              AND sector_type IN ('ths_concept','ths_concept_derived')
            ORDER BY main_net {direction} NULLS LAST LIMIT 10
        """
        try:
            result["sector_inflow"] = _fetch_dicts(con, sector_sql.format(direction="DESC"), [trade_date])
            result["sector_outflow"] = _fetch_dicts(con, sector_sql.format(direction="ASC"), [trade_date])
            industry_sql = sector_sql.replace(
                "sector_type IN ('ths_concept','ths_concept_derived')",
                "sector_type = 'em_industry'",
            )
            result["industry_inflow"] = _fetch_dicts(con, industry_sql.format(direction="DESC"), [trade_date])
            result["industry_outflow"] = _fetch_dicts(con, industry_sql.format(direction="ASC"), [trade_date])
        except Exception:
            pass

    if table_exists(con, "v_default_concept_stock_history") and table_exists(con, "v_limit_pool"):
        result["sector_limit_up"] = _rows(
            con,
            "v_default_concept_stock_history",
            """
            SELECT h.concept_code AS sector_code, max(h.concept_name) AS sector_name,
                   count(DISTINCT l.stock_code) AS limit_up_count,
                   string_agg(DISTINCT coalesce(l.stock_name,h.stock_name), ', ' ORDER BY coalesce(l.stock_name,h.stock_name)) AS limit_up_stocks
            FROM v_default_concept_stock_history h
            JOIN v_limit_pool l
              ON l.stock_code=regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '')
             AND l.trade_date=?
            WHERE h.trade_date=(
                SELECT max(trade_date) FROM v_default_concept_stock_history
                WHERE trade_date<=CAST(? AS DATE)
            )
            GROUP BY h.concept_code
            ORDER BY limit_up_count DESC, sector_name
            """,
            [trade_date, trade_date],
        )

    if table_exists(con, "stock_candidate_score"):
        result["candidate_picks"] = _rows(
            con,
            "stock_candidate_score",
            """
            WITH provider_ranked AS (
                SELECT stock_code, main_net,
                       row_number() OVER (
                         PARTITION BY stock_code ORDER BY CASE provider
                           WHEN 'eastmoney_market' THEN 1
                           WHEN 'eastmoney_intraday_clist_delay' THEN 2
                           WHEN 'eastmoney_intraday_clist' THEN 3
                           WHEN 'tushare' THEN 4
                           WHEN 'tushare_relay' THEN 4
                           WHEN 'kpl' THEN 5
                           ELSE 9 END,
                         fetched_at DESC NULLS LAST
                       ) AS provider_rank
                FROM multi_source_stock_flow
                WHERE source_date=CAST(? AS DATE) AND coalesce(is_stale,FALSE)=FALSE
            ), flow AS (
                SELECT stock_code,main_net,
                       row_number() OVER (
                         ORDER BY main_net DESC NULLS LAST,stock_code
                       ) AS flow_rank
                FROM provider_ranked
                WHERE provider_rank=1
            )
            SELECT s.stock_code, s.stock_name, s.score, s.source, s.sector_code,
                   f.main_net, f.flow_rank,
                   CASE WHEN s.source='limit_pool' THEN 'research_only_limit_pool' ELSE 'research_only' END AS selection_status
            FROM stock_candidate_score s
            LEFT JOIN flow f ON f.stock_code=s.stock_code
            WHERE s.trade_date=?
            ORDER BY s.score DESC NULLS LAST, f.main_net DESC NULLS LAST
            LIMIT 20
            """,
            [trade_date, trade_date],
        )

    # Prefer the materialized feature surface.  It uses trading-row windows,
    # stable provider priority and exposes both persistent inflow and outflow.
    if table_exists(con, "qlib_stock_flow_features"):
        try:
            persistence_sql = """
                SELECT stock_code, observed_days_20d, positive_days_20d AS positive_days,
                       main_net_3d, main_net_5d, main_net_10d,
                       main_net_20d AS twenty_day_main_net,
                       flow_acceleration_5d, trade_date AS latest_date, provider
                FROM qlib_stock_flow_features
                WHERE trade_date=CAST(? AS DATE)
                ORDER BY main_net_20d {direction} NULLS LAST, stock_code
                LIMIT 50
            """
            result["stock_flow_persistence"] = _fetch_dicts(
                con, persistence_sql.format(direction="DESC"), [trade_date]
            )
            result["stock_flow_persistence_outflow"] = _fetch_dicts(
                con, persistence_sql.format(direction="ASC"), [trade_date]
            )
        except Exception:
            result["stock_flow_persistence"] = []
            result["stock_flow_persistence_outflow"] = []

    # Compatibility fallback for databases that have not built the feature
    # tables yet.  It is intentionally narrower and marked by the report as
    # legacy persistence.
    if table_exists(con, "multi_source_stock_flow") and result["stock_inflow"] and not result["stock_flow_persistence"]:
        codes = [str(row.get("stock_code")) for row in result["stock_inflow"][:10] if row.get("stock_code")]
        if codes:
            placeholders = ",".join("?" for _ in codes)
            try:
                result["stock_flow_persistence"] = _fetch_dicts(
                    con,
                    f"""
                    WITH ranked AS (
                        SELECT stock_code, source_date, main_net,
                               row_number() OVER (
                                 PARTITION BY stock_code, source_date
                                 ORDER BY fetched_at DESC NULLS LAST
                               ) AS rn
                        FROM multi_source_stock_flow
                        WHERE stock_code IN ({placeholders})
                          AND source_date BETWEEN CAST(? AS DATE) - INTERVAL 20 DAY AND CAST(? AS DATE)
                          AND coalesce(is_stale,FALSE)=FALSE
                    )
                    SELECT stock_code, count(*) AS observed_days,
                           sum(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) AS positive_days,
                           round(sum(main_net), 0) AS twenty_day_main_net,
                           max(source_date) AS latest_date
                    FROM ranked WHERE rn=1
                    GROUP BY stock_code
                    ORDER BY positive_days DESC, twenty_day_main_net DESC
                    """,
                    [*codes, trade_date, trade_date],
                )
            except Exception:
                result["stock_flow_persistence"] = []

    if table_exists(con, "qlib_sector_flow_features"):
        try:
            persistence_sql = """
                SELECT sector_code, max(sector_name) AS sector_name,
                       observed_days_20d, positive_days_20d AS positive_days,
                       main_net_3d, main_net_5d, main_net_10d,
                       main_net_20d AS twenty_day_main_net,
                       flow_acceleration_5d, trade_date AS latest_date,
                       sector_type, provider
                FROM qlib_sector_flow_features
                WHERE trade_date=CAST(? AS DATE)
                  AND sector_type IN ('ths_concept','ths_concept_derived')
                GROUP BY sector_code, observed_days_20d, positive_days_20d,
                         main_net_3d, main_net_5d, main_net_10d, main_net_20d,
                         flow_acceleration_5d, trade_date, sector_type, provider
                ORDER BY twenty_day_main_net {direction} NULLS LAST, sector_code
                LIMIT 50
            """
            result["sector_flow_persistence"] = _fetch_dicts(
                con, persistence_sql.format(direction="DESC"), [trade_date]
            )
            result["sector_flow_persistence_outflow"] = _fetch_dicts(
                con, persistence_sql.format(direction="ASC"), [trade_date]
            )
        except Exception:
            result["sector_flow_persistence"] = []
            result["sector_flow_persistence_outflow"] = []

    if table_exists(con, "multi_source_sector_flow") and result["sector_inflow"] and not result["sector_flow_persistence"]:
        codes = [str(row.get("sector_code")) for row in result["sector_inflow"][:10] if row.get("sector_code")]
        if codes:
            placeholders = ",".join("?" for _ in codes)
            try:
                result["sector_flow_persistence"] = _fetch_dicts(
                    con,
                    f"""
                    WITH ranked AS (
                        SELECT sector_code, sector_name, source_date, main_net,
                               row_number() OVER (
                                 PARTITION BY sector_code, source_date
                                 ORDER BY fetched_at DESC NULLS LAST
                               ) AS rn
                        FROM multi_source_sector_flow
                        WHERE sector_code IN ({placeholders})
                          AND source_date BETWEEN CAST(? AS DATE) - INTERVAL 20 DAY AND CAST(? AS DATE)
                          AND coalesce(is_stale,FALSE)=FALSE
                          AND sector_type IN ('ths_concept','ths_concept_derived')
                    )
                    SELECT sector_code, max(sector_name) AS sector_name,
                           count(*) AS observed_days,
                           sum(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) AS positive_days,
                           round(sum(main_net), 0) AS twenty_day_main_net,
                           max(source_date) AS latest_date
                    FROM ranked WHERE rn=1
                    GROUP BY sector_code
                    ORDER BY positive_days DESC, twenty_day_main_net DESC
                    """,
                    [*codes, trade_date, trade_date],
                )
            except Exception:
                result["sector_flow_persistence"] = []

    # 龙虎榜 is a post-market review input.  Keep it separate from executable
    # candidates and show the source date explicitly when the feed is stale.
    if table_exists(con, "lhb_list"):
        result["lhb"] = _rows(
            con,
            "lhb_list",
            """
            SELECT date, stock_code, stock_name, change_pct, reason,
                   buy_amount, sell_amount, net_amount
            FROM lhb_list
            WHERE date=CAST(? AS DATE)
            ORDER BY abs(net_amount) DESC NULLS LAST
            LIMIT 20
            """,
            [trade_date],
        )

    stock_meta = result.get("stock_flow_meta") or {}
    sector_meta = result.get("sector_flow_meta") or {}
    if float(stock_meta.get("batch_coverage_pct") or 0) < 99.5:
        result["coverage_alerts"].append(
            f"个股资金流覆盖 {stock_meta.get('batch_coverage_pct', 0)}%，低于 99.5%"
        )
    if float(sector_meta.get("batch_coverage_pct") or 0) < 99.5:
        result["coverage_alerts"].append(
            f"板块资金流覆盖 {sector_meta.get('batch_coverage_pct', 0)}%，低于 99.5%"
        )
    return result


_BROAD_TRAIL_CONCEPTS = (
    "融资融券", "沪股通", "深股通", "国企改革", "富时罗素", "标普道琼斯",
    "融资融券概念", "转融通标的", "融资融券标的", "深股通50", "沪股通50",
)


def _concept_limit_up_review(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """Build the concept -> limit-up stock drill-down used by the review page.

    The THS membership snapshot can legitimately lag the close date by one
    session.  We therefore select the latest quality-gated membership snapshot
    not later than ``trade_date`` and join it to the same-date limit pool.  The
    mainline view is only used to rank concepts; limit-up counts and stock rows
    are recomputed from the two source surfaces so NULL values in the older
    mainline materialization cannot become fake zeros on the page.
    """
    result: dict[str, Any] = {
        "status": "unavailable",
        "membership_date": None,
        "limit_date": trade_date,
        "groups": [],
        "large_concepts": 0,
    }
    required = ("v_default_concept_stock_history", "v_limit_pool")
    if any(not table_exists(con, relation) for relation in required):
        result["message"] = "THS concept membership or same-date limit pool is unavailable"
        return result

    def _code(value: Any) -> str:
        raw = str(value or "").split(".", 1)[0]
        return raw.zfill(6) if raw.isdigit() else raw

    try:
        member_date_row = con.execute(
            "SELECT max(trade_date) FROM v_default_concept_stock_history "
            "WHERE trade_date<=CAST(? AS DATE)",
            [trade_date],
        ).fetchone()
        member_date = member_date_row[0] if member_date_row else None
        if not member_date:
            result["message"] = "no THS membership snapshot not later than the review date"
            return result
        result["membership_date"] = str(member_date)
        membership_age = (date.fromisoformat(str(trade_date)[:10]) - date.fromisoformat(str(member_date)[:10])).days
        result["membership_age_days"] = membership_age
        if membership_age > 7:
            result["message"] = f"THS membership snapshot is stale by {membership_age} days"
            result["membership_stale"] = True
            return result

        broad = list(_BROAD_TRAIL_CONCEPTS)
        broad_ph = ",".join("?" for _ in broad)
        members = _fetch_dicts(
            con,
            f"""
            SELECT concept_code, max(concept_name) AS concept_name,
                   regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                   max(stock_name) AS stock_name
            FROM v_default_concept_stock_history
            WHERE trade_date = CAST(? AS DATE)
              AND concept_code LIKE 'THS-%'
              AND coalesce(concept_name, '') NOT IN ({broad_ph})
            GROUP BY concept_code, regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '')
            """,
            [member_date, *broad],
        )
        scores = []
        if table_exists(con, "v_theme_mainline_evidence"):
            scores = _fetch_dicts(
                con,
                """
                SELECT sector_code AS concept_code,
                       max(strength_value) AS strength_value,
                       max(main_net_inflow) AS main_net_inflow,
                       max(mainline_score) AS mainline_score
                FROM v_theme_mainline_evidence
                WHERE CAST(trade_date AS DATE) = CAST(? AS DATE)
                  AND sector_code LIKE 'THS-%'
                GROUP BY sector_code
                """,
                [trade_date],
            )
        limits = _fetch_dicts(
            con,
            """
            SELECT trade_date, board_level, stock_code, stock_name,
                   limit_up_time, fetched_at
            FROM v_limit_pool
            WHERE trade_date=?
            """,
            [trade_date],
        )
        # Provider ``board_level`` is evidence, not the canonical streak
        # calculation.  Recompute the same-date levels from the trading
        # calendar so concept summaries, the ladder and detail cards cannot
        # disagree when an upstream snapshot resets or regresses a label.
        level_lookup = _derived_limit_board_levels(
            con,
            trade_date,
            [str(item.get("stock_code") or "") for item in limits],
        )
        for item in limits:
            key = (
                _code(item.get("stock_code")),
                str(item.get("trade_date") or trade_date)[:10],
            )
            if key in level_lookup:
                item["board_level"] = level_lookup[key]
    except Exception as exc:
        result["message"] = f"concept-limit-up join failed: {type(exc).__name__}"
        return result

    score_by_code = {str(row["concept_code"]): dict(row) for row in scores}
    name_by_code: dict[str, str] = {}
    members_by_stock: dict[str, set[str]] = {}
    member_counts: dict[str, set[str]] = {}
    for row in members:
        concept_code = str(row.get("concept_code") or "")
        stock_code = _code(row.get("stock_code"))
        if not concept_code or not stock_code:
            continue
        name_by_code[concept_code] = str(row.get("concept_name") or concept_code)
        members_by_stock.setdefault(stock_code, set()).add(concept_code)
        member_counts.setdefault(concept_code, set()).add(stock_code)

    grouped: dict[str, dict[str, Any]] = {}
    for row in limits:
        stock_code = _code(row.get("stock_code"))
        for concept_code in members_by_stock.get(stock_code, set()):
            size = len(member_counts.get(concept_code, set()))
            score = score_by_code.get(concept_code) or {}
            group = grouped.setdefault(
                concept_code,
                {
                    "concept_code": concept_code,
                    "concept_name": name_by_code.get(concept_code, concept_code),
                    "strength_value": score.get("strength_value"),
                    "main_net_inflow": score.get("main_net_inflow"),
                    "mainline_score": score.get("mainline_score"),
                    "member_count": size,
                    "limit_up_count": 0,
                    "max_board": None,
                    "limit_up_stocks": [],
                },
            )
            if not any(item.get("stock_code") == stock_code for item in group["limit_up_stocks"]):
                group["limit_up_stocks"].append(
                    {
                        "stock_code": stock_code,
                        "stock_name": row.get("stock_name") or row.get("stock_name") or "",
                        "board_level": row.get("board_level"),
                        "limit_up_time": row.get("limit_up_time"),
                    }
                )

    groups: list[dict[str, Any]] = []
    for group in grouped.values():
        stocks = group["limit_up_stocks"]
        if not stocks:
            continue
        stocks.sort(
            key=lambda item: (
                -(float(item.get("board_level")) if item.get("board_level") is not None else -1),
                str(item.get("stock_code") or ""),
            )
        )
        group["limit_up_count"] = len(stocks)
        group["max_board"] = stocks[0].get("board_level") if stocks else None
        groups.append(group)

    result["large_concepts"] = sum(
        1 for code, names in member_counts.items()
        if len(names) > 800
    )
    groups.sort(
        key=lambda item: (
            -int(item.get("limit_up_count") or 0),
            -(float(item.get("max_board")) if item.get("max_board") is not None else -1),
            -(float(item.get("mainline_score")) if item.get("mainline_score") is not None else -1),
            str(item.get("concept_name") or ""),
        )
    )
    # Keep every quality-gated concept with same-date limit-up members.  The
    # web renderer provides a scrollable selector; truncating here made the
    # page look as if THS had only a handful of concepts.
    result["groups"] = groups
    result["status"] = "ready" if result["groups"] else "degraded"
    if not result["groups"]:
        result["message"] = "no candidate concept has same-date limit-up members"
    return result


def _market_context_review(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """Collect same-date breadth, auction, limit-up ecology and LHB evidence."""
    context: dict[str, Any] = {
        "breadth": [], "limit_summary": [], "auction": [],
        "limit_ladder": [], "lhb_summary": {},
    }
    if table_exists(con, "market_rise_fall"):
        context["breadth"] = _rows(
            con, "market_rise_fall",
            """SELECT date, limit_up_count, limit_down_count, broken_limit_up_count,
                    blown_limit_up_count, blown_limit_up_rate, raw_field_5, source_kind
             FROM market_rise_fall WHERE date=CAST(? AS DATE) ORDER BY updated_at DESC LIMIT 1""",
            [trade_date],
        )
    if table_exists(con, "daily_summary"):
        context["breadth"] += _rows(
            con, "daily_summary",
            """SELECT date, limit_up_count, limit_down_count, rise_count, fall_count,
                    consecutive_count, source_kind
             FROM daily_summary WHERE date=CAST(? AS DATE) LIMIT 1""",
            [trade_date],
        )
    if table_exists(con, "market_limit_up_down_summary"):
        context["limit_summary"] = _rows(
            con, "market_limit_up_down_summary",
            """SELECT date, limit_up_count, limit_down_count, actual_limit_up_count,
                    actual_limit_down_count, blown_limit_up_rate
             FROM market_limit_up_down_summary WHERE date=CAST(? AS DATE) LIMIT 1""",
            [trade_date],
        )
    if table_exists(con, "auction_bidding_anomaly"):
        context["auction"] = _rows(
            con, "auction_bidding_anomaly",
            """SELECT date, count(*) AS anomaly_count,
                    count(DISTINCT stock_code) AS stock_count,
                    max(fetched_at) AS fetched_at
             FROM auction_bidding_anomaly WHERE date=CAST(? AS DATE)
             GROUP BY date""",
            [trade_date],
        )
    if table_exists(con, "ladder_realtime_boards"):
        cols = set(table_columns(con, "ladder_realtime_boards"))
        date_col = "trade_date" if "trade_date" in cols else "date" if "date" in cols else None
        if date_col:
            context["limit_ladder"] = _rows(
                con, "ladder_realtime_boards",
                f"SELECT * FROM ladder_realtime_boards WHERE {date_col}=CAST(? AS DATE) LIMIT 20",
                [trade_date],
            )
    if table_exists(con, "lhb_list"):
        row = con.execute(
            "SELECT count(*), count(DISTINCT stock_code), max(fetched_at) "
            "FROM lhb_list WHERE date=CAST(? AS DATE)", [trade_date]
        ).fetchone()
        context["lhb_summary"] = {
            "rows": int(row[0] or 0), "stocks": int(row[1] or 0),
            "fetched_at": str(row[2]) if row and row[2] else None,
        }
    return context


def _data_source_review(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """Expose same-date provider checkpoints and research evidence in the review."""
    result: dict[str, Any] = {
        "tushare": [], "kline": [], "ths": {}, "flow_features": {}, "outcomes": 0, "qlib": [], "strategy": [],
    }
    if table_exists(con, "history_fetch_checkpoint"):
        result["tushare"] = _rows(
            con,
            "history_fetch_checkpoint",
            """SELECT dataset, status, rows_written, attempts, last_error, updated_at
               FROM history_fetch_checkpoint
               WHERE trade_date=CAST(? AS DATE)
                 AND dataset IN ('daily','daily_basic','adj_factor','moneyflow','industry_flow','ths_concept_snapshot')
               ORDER BY dataset""",
            [trade_date],
        )
        expected_datasets = ("daily", "daily_basic", "adj_factor", "moneyflow", "industry_flow", "ths_concept_snapshot")
        found = {str(row.get("dataset")) for row in result["tushare"]}
        result["tushare"].extend(
            {"dataset": name, "status": "missing", "rows_written": 0, "attempts": 0,
             "last_error": "no same-date checkpoint", "updated_at": None}
            for name in expected_datasets if name not in found
        )
        result["tushare"].sort(key=lambda row: str(row.get("dataset") or ""))
    for relation in ("kline", "v_kline_daily"):
        if table_exists(con, relation):
            cols = set(table_columns(con, relation))
            date_col = "date" if "date" in cols else "trade_date" if "trade_date" in cols else None
            if date_col:
                latest = con.execute(f"SELECT max({date_col}) FROM {relation}").fetchone()[0]
                same_date = con.execute(
                    f"SELECT count(*) FROM {relation} WHERE CAST({date_col} AS DATE)=CAST(? AS DATE)",
                    [trade_date],
                ).fetchone()[0]
                result["kline"].append({"relation": relation, "latest": str(latest) if latest else None, "same_date_rows": int(same_date or 0)})
    if table_exists(con, "ths_concept_member_checkpoint"):
        latest = con.execute(
            "SELECT max(trade_date) FROM ths_concept_member_checkpoint WHERE trade_date<=CAST(? AS DATE)",
            [trade_date],
        ).fetchone()[0]
        if latest:
            row = con.execute(
                "SELECT count(*), sum(CASE WHEN status='success' THEN 1 ELSE 0 END), "
                "sum(CASE WHEN status<>'success' THEN 1 ELSE 0 END) "
                "FROM ths_concept_member_checkpoint WHERE trade_date=?", [latest]
            ).fetchone()
            raw_concepts = int(con.execute(
                "SELECT count(DISTINCT concept_code) FROM ths_concept_daily WHERE trade_date=?", [latest]
            ).fetchone()[0] or 0) if table_exists(con, "ths_concept_daily") else 0
            raw_members = int(con.execute(
                "SELECT count(*) FROM ths_concept_stock_history WHERE trade_date=?", [latest]
            ).fetchone()[0] or 0) if table_exists(con, "ths_concept_stock_history") else 0
            usable_concepts = int(con.execute(
                "SELECT count(DISTINCT concept_code) FROM v_default_concept_daily WHERE trade_date=?", [latest]
            ).fetchone()[0] or 0) if table_exists(con, "v_default_concept_daily") else 0
            usable_members = int(con.execute(
                "SELECT count(*) FROM v_default_concept_stock_history WHERE trade_date=?", [latest]
            ).fetchone()[0] or 0) if table_exists(con, "v_default_concept_stock_history") else 0
            result["ths"] = {"trade_date": str(latest), "concepts": raw_concepts, "members": raw_members,
                           "usable_concepts": usable_concepts, "usable_members": usable_members,
                           "stale_or_partial_concepts": max(0, raw_concepts - usable_concepts),
                           "stale_or_partial_members": max(0, raw_members - usable_members),
                           "checkpoint_rows": int(row[0] or 0), "success": int(row[1] or 0),
                           "partial": int(row[2] or 0)}
    if table_exists(con, "operator_trade_outcome"):
        result["outcomes"] = int(con.execute(
            "SELECT count(*) FROM operator_trade_outcome WHERE trade_date=CAST(? AS DATE)", [trade_date]
        ).fetchone()[0] or 0)
    feature_rows = {}
    for relation in ("qlib_stock_flow_features", "qlib_sector_flow_features"):
        if table_exists(con, relation):
            row = con.execute(
                f"SELECT count(*), count(DISTINCT trade_date), max(trade_date), max(feature_version) FROM {relation} WHERE trade_date<=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
            feature_rows[relation] = {
                "rows": int(row[0] or 0),
                "dates": int(row[1] or 0),
                "latest": str(row[2]) if row[2] else None,
                "version": row[3],
            }
    result["flow_features"] = feature_rows
    if table_exists(con, "qlib_shadow_evaluation"):
        cols = set(table_columns(con, "qlib_shadow_evaluation"))
        qlib_select = ["model_id", "'shadow' AS stage", "sample_count", "hit_rate"]
        if "avg_forward_return" in cols:
            qlib_select.append("avg_forward_return AS avg_forward_return_pct")
        elif "top_quantile_return" in cols:
            qlib_select.append("top_quantile_return AS avg_forward_return_pct")
        else:
            qlib_select.append("NULL AS avg_forward_return_pct")
        qlib_select.append("ic" if "ic" in cols else "NULL AS ic")
        qlib_select.append("rank_ic" if "rank_ic" in cols else "NULL AS rank_ic")
        qlib_select.append("top_bottom_spread" if "top_bottom_spread" in cols else "NULL AS top_bottom_spread")
        qlib_select.append("max_drawdown" if "max_drawdown" in cols else "NULL AS max_drawdown")
        qlib_select.append("'disabled' AS signal_impact")
        order_col = "sample_end" if "sample_end" in cols else "model_id"
        result["qlib"] = _rows(
            con, "qlib_shadow_evaluation",
            f"SELECT {', '.join(qlib_select)} FROM qlib_shadow_evaluation ORDER BY {order_col} DESC NULLS LAST LIMIT 10",
            [],
        )
    if table_exists(con, "strategy_backtest_result"):
        cols = set(table_columns(con, "strategy_backtest_result"))
        strategy_select = ["strategy_id", "stage", "sample_count", "win_rate"]
        strategy_select.append("avg_return AS avg_return_pct" if "avg_return" in cols else "NULL AS avg_return_pct")
        strategy_select.append("'not_verified' AS verdict")
        order_col = "sample_end" if "sample_end" in cols else "strategy_id"
        result["strategy"] = _rows(
            con, "strategy_backtest_result",
            f"SELECT {', '.join(strategy_select)} FROM strategy_backtest_result ORDER BY {order_col} DESC NULLS LAST LIMIT 10",
            [],
        )
    return result


_REGIME_NOTES = {
    "冰点": "极度恐慌，无赚钱效应。管住手，等待新周期试错。",
    "启动": "新题材出现，局部回暖。轻仓试错，聚焦新题材的破局龙。",
    "主升": "赚钱效应爆棚，主线确立。重仓只做主线龙头和前排核心。",
    "高潮": "鸡犬升天，缩量加速。持股兑现，不新开仓，卖出后排跟风。",
    "退潮": "亏钱效应弥漫，大面频出。清仓防守，严禁接力和反包。",
    "震荡": "多空拉锯。轻仓观察，等主线重新排出来再动手。",
}

_REGIME_FORECAST = {
    "冰点": "转折（关注新题材启动）",
    "启动": "延续",
    "主升": "延续",
    "高潮": "转折（注意退潮风险）",
    "退潮": "观望（等待企稳）",
    "震荡": "观望",
}


def _regime_key(name: str) -> str:
    text = str(name or "")
    for key in ("冰点", "启动", "主升", "高潮", "退潮", "震荡"):
        if key in text:
            return key
    return ""


def _ecology_review(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """A-share style short-term ecology: named ladder, yday premium, failed continuation."""
    result: dict[str, Any] = {
        "prev_date": "",
        "ladder_groups": [],
        "leader": None,
        "yday": {},
        "broken": [],
        "signals": [],
        "playbook": {},
    }
    prev_date = ""
    if table_exists(con, "v_limit_pool"):
        try:
            row = con.execute(
                "SELECT max(trade_date) FROM v_limit_pool WHERE CAST(trade_date AS DATE) < CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
            prev_date = str(row[0]) if row and row[0] else ""
        except Exception:
            prev_date = ""
    result["prev_date"] = prev_date

    if table_exists(con, "v_limit_pool"):
        try:
            rows = _fetch_dicts(
                con,
                """
                SELECT board_level, stock_code, stock_name, limit_up_time
                FROM v_limit_pool
                WHERE CAST(trade_date AS DATE) = CAST(? AS DATE)
                ORDER BY board_level DESC, stock_code
                """,
                [trade_date],
            )
        except Exception:
            rows = []
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            height = int(row.get("board_level") or 0)
            grouped.setdefault(height, []).append(row)
        groups = []
        for height in sorted(grouped, reverse=True):
            stocks = grouped[height]
            note = "龙头" if height >= 5 else "中坚" if height >= 3 else "首板" if height == 1 else ""
            groups.append({
                "height": height,
                "count": len(stocks),
                "note": note,
                "names": [s.get("stock_name") or s.get("stock_code") for s in stocks],
                "stocks": stocks[:20],
            })
        result["ladder_groups"] = groups
        if groups:
            top = groups[0]
            result["leader"] = (top.get("stocks") or [{}])[0]

    if prev_date and table_exists(con, "v_limit_pool") and table_exists(con, "multi_source_stock_flow"):
        try:
            yday = _fetch_dicts(
                con,
                """
                WITH y AS (
                    SELECT stock_code, stock_name, board_level
                    FROM v_limit_pool WHERE CAST(trade_date AS DATE) = CAST(? AS DATE)
                ), t AS (
                    SELECT stock_code FROM v_limit_pool WHERE CAST(trade_date AS DATE) = CAST(? AS DATE)
                ), f AS (
                    SELECT stock_code, change_pct,
                           row_number() OVER (PARTITION BY stock_code ORDER BY fetched_at DESC) AS rn
                    FROM multi_source_stock_flow
                    WHERE source_date = CAST(? AS DATE) AND coalesce(is_stale, FALSE) = FALSE
                )
                SELECT count(*) AS n,
                       avg(f.change_pct) AS avg_ret,
                       100.0 * sum(CASE WHEN f.change_pct > 0 THEN 1 ELSE 0 END) / nullif(count(*), 0) AS pos_rate,
                       sum(CASE WHEN t.stock_code IS NOT NULL THEN 1 ELSE 0 END) AS still_limit_up,
                       sum(CASE WHEN f.change_pct <= -9.5 THEN 1 ELSE 0 END) AS limit_down,
                       max(f.change_pct) AS max_ret,
                       min(f.change_pct) AS min_ret
                FROM y
                LEFT JOIN f ON f.stock_code = y.stock_code AND f.rn = 1
                LEFT JOIN t ON t.stock_code = y.stock_code
                """,
                [prev_date, trade_date, trade_date],
            )
            result["yday"] = yday[0] if yday else {}
            splits = _fetch_dicts(
                con,
                """
                WITH y AS (
                    SELECT stock_code, board_level FROM v_limit_pool WHERE CAST(trade_date AS DATE) = CAST(? AS DATE)
                ), f AS (
                    SELECT stock_code, change_pct,
                           row_number() OVER (PARTITION BY stock_code ORDER BY fetched_at DESC) AS rn
                    FROM multi_source_stock_flow
                    WHERE source_date = CAST(? AS DATE) AND coalesce(is_stale, FALSE) = FALSE
                )
                SELECT CASE WHEN y.board_level = 1 THEN 'first' ELSE 'multi' END AS grp,
                       count(*) AS n, avg(f.change_pct) AS avg_ret
                FROM y LEFT JOIN f ON f.stock_code = y.stock_code AND f.rn = 1
                GROUP BY 1
                """,
                [prev_date, trade_date],
            )
            for item in splits:
                if item.get("grp") == "first":
                    result["yday"]["first_avg"] = item.get("avg_ret")
                    result["yday"]["first_n"] = item.get("n")
                else:
                    result["yday"]["multi_avg"] = item.get("avg_ret")
                    result["yday"]["multi_n"] = item.get("n")
            result["broken"] = _fetch_dicts(
                con,
                """
                WITH y AS (
                    SELECT stock_code, stock_name, board_level
                    FROM v_limit_pool
                    WHERE CAST(trade_date AS DATE) = CAST(? AS DATE) AND board_level >= 2
                ), t AS (
                    SELECT stock_code FROM v_limit_pool WHERE CAST(trade_date AS DATE) = CAST(? AS DATE)
                ), f AS (
                    SELECT stock_code, change_pct,
                           row_number() OVER (PARTITION BY stock_code ORDER BY fetched_at DESC) AS rn
                    FROM multi_source_stock_flow
                    WHERE source_date = CAST(? AS DATE) AND coalesce(is_stale, FALSE) = FALSE
                )
                SELECT y.stock_code, y.stock_name, y.board_level, f.change_pct
                FROM y
                LEFT JOIN t ON t.stock_code = y.stock_code
                LEFT JOIN f ON f.stock_code = y.stock_code AND f.rn = 1
                WHERE t.stock_code IS NULL
                ORDER BY f.change_pct ASC NULLS LAST
                LIMIT 12
                """,
                [prev_date, trade_date, trade_date],
            )
        except Exception:
            pass
    result["yday"]["prev_date"] = prev_date
    return result


def _derived_limit_board_levels(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    stock_codes: list[str],
) -> dict[tuple[str, str], int]:
    """Derive consecutive limit-up levels from dates, not a provider label.

    ``v_limit_pool.board_level`` is useful as source evidence, but some of the
    historical provider snapshots reset or regress while a stock is still
    hitting limit-up.  The concept trail needs a reproducible value, so derive
    the level from the actual limit-up dates and the exchange trading calendar.
    The lookup includes dates before the visible window so a streak crossing
    the window boundary is not incorrectly restarted at one.
    """
    if not stock_codes or not table_exists(con, "v_limit_pool"):
        return {}
    normalized = sorted({str(code).split(".", 1)[0] for code in stock_codes if code})
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    try:
        rows = _fetch_dicts(
            con,
            f"""
            SELECT DISTINCT CAST(trade_date AS DATE) AS trade_date,
                   regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code
            FROM v_limit_pool
            WHERE CAST(trade_date AS DATE) <= CAST(? AS DATE)
              AND regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') IN ({placeholders})
            ORDER BY trade_date, stock_code
            """,
            [trade_date, *normalized],
        )
    except Exception:
        return {}
    if not rows:
        return {}

    all_dates = sorted({str(row.get("trade_date") or "")[:10] for row in rows if row.get("trade_date")})
    calendar_dates: list[str] = []
    if table_exists(con, "tushare_trade_cal") and all_dates:
        try:
            calendar_rows = con.execute(
                """
                SELECT CAST(cal_date AS DATE) AS trade_date
                FROM tushare_trade_cal
                WHERE is_open = 1
                  AND CAST(cal_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                ORDER BY cal_date
                """,
                [all_dates[0], trade_date],
            ).fetchall()
            calendar_dates = [str(row[0])[:10] for row in calendar_rows]
        except Exception:
            calendar_dates = []
    open_dates = calendar_dates or all_dates
    previous_open = {open_dates[index]: open_dates[index - 1] for index in range(1, len(open_dates))}

    by_stock: dict[str, list[str]] = {}
    for row in rows:
        code = str(row.get("stock_code") or "")[:6]
        day = str(row.get("trade_date") or "")[:10]
        if code and day:
            by_stock.setdefault(code, []).append(day)

    levels: dict[tuple[str, str], int] = {}
    for code, dates in by_stock.items():
        streak = 0
        previous_day = None
        for day in sorted(set(dates)):
            streak = streak + 1 if previous_day and previous_open.get(day) == previous_day else 1
            levels[(code, day)] = streak
            previous_day = day
    return levels


def _sector_trail_review(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    days: int = 20,
    top_per_day: int = 0,
) -> dict[str, Any]:
    """Multi-day THS concept trail: limit-up count + change + flow per session.

    This is the akshare_skill DailyReview contract: every quality-gated
    concept is retained, each date is a card ranked by same-date limit-up
    members, and the selected concept also carries the union of stocks that
    were limit-up on any date in the review window.
    """
    empty = {"dates": [], "sectors": [], "membership_date": None, "status": "unavailable"}
    if not table_exists(con, "v_limit_pool") or not table_exists(con, "v_default_concept_stock_history"):
        empty["message"] = "涨停池或概念成分不可用"
        return empty
    try:
        date_rows = con.execute(
            """
            SELECT d FROM (
                SELECT DISTINCT CAST(trade_date AS DATE) AS d
                FROM v_limit_pool
                WHERE CAST(trade_date AS DATE) <= CAST(? AS DATE)
            ) t
            ORDER BY d DESC
            LIMIT ?
            """,
            [trade_date, days],
        ).fetchall()
        dates = [str(row[0]) for row in date_rows]
        if not dates:
            empty["message"] = "涨停池没有可对比的历史日期"
            return empty
        broad = list(_BROAD_TRAIL_CONCEPTS)
        broad_ph = ",".join("?" for _ in broad)
        date_ph = ",".join("?" for _ in dates)
        if table_exists(con, "multi_source_sector_flow"):
            flow_sql = f"""
                flow AS (
                    SELECT CAST(source_date AS DATE) AS d,
                           CAST(sector_code AS VARCHAR) AS sector_code,
                           change_pct, main_net
                    FROM (
                        SELECT source_date, sector_code, change_pct, main_net,
                               row_number() OVER (
                                   PARTITION BY source_date, sector_code
                                   ORDER BY fetched_at DESC NULLS LAST
                               ) AS rn
                        FROM multi_source_sector_flow
                        WHERE source_date IN ({date_ph})
                          AND coalesce(is_stale, FALSE) = FALSE
                          AND sector_type IN ('ths_concept', 'ths_concept_derived')
                    ) ranked
                    WHERE rn = 1
                )
            """
        else:
            flow_sql = """
                flow AS (
                    SELECT CAST(NULL AS DATE) AS d,
                           CAST(NULL AS VARCHAR) AS sector_code,
                           CAST(NULL AS DOUBLE) AS change_pct,
                           CAST(NULL AS DOUBLE) AS main_net
                    WHERE FALSE
                )
            """
        rows = _fetch_dicts(
            con,
            f"""
            WITH lu AS (
                 SELECT CAST(trade_date AS DATE) AS d,
                        regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code
                 FROM v_limit_pool
                 WHERE CAST(trade_date AS DATE) IN ({date_ph})
                 GROUP BY 1, 2
             ), requested_dates AS (
                 SELECT DISTINCT d FROM lu
             ), concept_snapshot_dates AS (
                 SELECT rd.d,
                        CAST(h.concept_code AS VARCHAR) AS concept_code,
                        max(CAST(h.trade_date AS DATE)) AS member_date
                 FROM requested_dates rd
                 JOIN v_default_concept_stock_history h
                   ON CAST(h.trade_date AS DATE) <= rd.d
                  AND h.concept_code LIKE 'THS-%'
                  AND coalesce(h.concept_name, '') NOT IN ({broad_ph})
                 GROUP BY rd.d, h.concept_code
             ), member_asof AS (
                 SELECT s.d, s.member_date, s.concept_code,
                        max(h.concept_name) AS concept_name,
                        regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '') AS stock_code
                 FROM concept_snapshot_dates s
                 JOIN v_default_concept_stock_history h
                   ON CAST(h.trade_date AS DATE) = s.member_date
                  AND CAST(h.concept_code AS VARCHAR) = s.concept_code
                 GROUP BY s.d, s.member_date, s.concept_code,
                          regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '')
            ), sizes AS (
                SELECT d, concept_code, member_date,
                       count(DISTINCT stock_code) AS member_count
                FROM member_asof
                GROUP BY d, concept_code, member_date
             ), {flow_sql}, counted AS (
                 SELECT m.d, m.concept_code, max(m.concept_name) AS concept_name,
                        m.member_date, z.member_count,
                        count(DISTINCT lu.stock_code) AS limit_up_count
                 FROM member_asof m
                 JOIN sizes z
                   ON z.d = m.d AND z.concept_code = m.concept_code
                   AND z.member_date = m.member_date
                  LEFT JOIN lu ON lu.d = m.d AND lu.stock_code = m.stock_code
                 GROUP BY m.d, m.concept_code, m.member_date, z.member_count
             ), component_stats AS (
                 SELECT m.d, m.concept_code, z.member_count,
                        count(DISTINCT CASE WHEN k.close IS NOT NULL AND k.close > 0 THEN m.stock_code END) AS valid_count,
                        avg(CASE WHEN k.close IS NOT NULL AND k.close > 0 THEN k.change_pct END) AS avg_pct,
                        sum(CASE WHEN k.close IS NOT NULL AND k.close > 0 THEN k.turnover ELSE 0 END) AS amount
                 FROM member_asof m
                 JOIN sizes z
                   ON z.d = m.d AND z.concept_code = m.concept_code
                   AND z.member_date = m.member_date
                 LEFT JOIN v_kline_daily k
                   ON CAST(k.trade_date AS DATE) = m.d
                  AND regexp_replace(CAST(k.stock_code AS VARCHAR), '[.].*$', '') = m.stock_code
                 GROUP BY m.d, m.concept_code, z.member_count
             )
            SELECT CAST(c.d AS VARCHAR) AS trade_date,
                   c.concept_code, c.concept_name, c.limit_up_count,
                   c.member_count, s.valid_count, s.avg_pct, s.amount,
                   CAST(c.member_date AS VARCHAR) AS membership_date,
                   coalesce(s.avg_pct, f.change_pct) AS change_pct,
                   f.change_pct AS flow_pct_chg, f.main_net
            FROM counted c
            LEFT JOIN component_stats s
              ON s.d = c.d AND s.concept_code = c.concept_code
             LEFT JOIN flow f
               ON f.d = c.d AND f.sector_code = c.concept_code
             WHERE coalesce(s.valid_count, 0) > 0 OR c.limit_up_count > 0
            """,
            [*dates, *broad, *dates] if table_exists(con, "multi_source_sector_flow") else [*dates, *broad],
        )
    except Exception as exc:
        empty["message"] = f"sector trail failed: {type(exc).__name__}"
        return empty

    member_dates = [str(row.get("membership_date") or "")[:10] for row in rows if row.get("membership_date")]
    member_date = max(member_dates) if member_dates else None
    if not member_date:
        empty["message"] = "没有不晚于复盘日的概念成分快照"
        return empty
    by_date: dict[str, list[dict[str, Any]]] = {d: [] for d in dates}
    for row in rows:
        day = str(row.get("trade_date") or "")[:10]
        if day in by_date:
            by_date[day].append(row)

    keep: set[str] = set()
    for day in dates:
        ranked = sorted(
            by_date.get(day) or [],
            key=lambda item: (
                -int(item.get("limit_up_count") or 0),
                -float(item.get("main_net") or 0),
            ),
        )
        selected = ranked if not top_per_day or top_per_day < 1 else ranked[:top_per_day]
        for item in selected:
            if item.get("concept_code"):
                keep.add(str(item["concept_code"]))

    sectors: list[dict[str, Any]] = []
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("concept_code") or "")
        if code not in keep:
            continue
        sector = by_code.get(code)
        if sector is None:
            sector = {
                "id": code,
                "name": row.get("concept_name") or code,
                "daily": {},
                "window_stocks": [],
            }
            by_code[code] = sector
            sectors.append(sector)
        day = str(row.get("trade_date") or "")[:10]
        sector["daily"][day] = {
            "limit_up": int(row.get("limit_up_count") or 0),
            "member_count": int(row.get("member_count") or 0),
            "valid_count": int(row.get("valid_count") or 0),
            "coverage_pct": round(
                float(row.get("valid_count") or 0) * 100.0 / float(row.get("member_count") or 1), 2
            ) if row.get("member_count") else None,
            "strength": row.get("avg_pct"),
            "amount": row.get("amount"),
            "pct_chg": row.get("change_pct"),
            "flow_pct_chg": row.get("flow_pct_chg"),
            "main_net": row.get("main_net"),
            "membership_date": str(row.get("membership_date") or "")[:10] or None,
            "stocks": [],
        }

    if keep and table_exists(con, "v_limit_pool"):
        keep_list = sorted(keep)
        keep_ph = ",".join("?" for _ in keep_list)
        date_ph = ",".join("?" for _ in dates)
        try:
            stock_rows = _fetch_dicts(
                con,
                f"""
                WITH lu AS (
                    SELECT CAST(trade_date AS DATE) AS d,
                           regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                           max(stock_name) AS stock_name,
                           max(board_level) AS board_level,
                           max(limit_up_time) AS limit_up_time
                    FROM v_limit_pool
                    WHERE CAST(trade_date AS DATE) IN ({date_ph})
                    GROUP BY 1, 2
                ), requested_dates AS (
                    SELECT DISTINCT d FROM lu
                ), concept_snapshot_dates AS (
                    SELECT rd.d, CAST(h.concept_code AS VARCHAR) AS concept_code,
                           max(CAST(h.trade_date AS DATE)) AS member_date
                    FROM requested_dates rd
                    JOIN v_default_concept_stock_history h
                      ON CAST(h.trade_date AS DATE) <= rd.d
                     AND h.concept_code IN ({keep_ph})
                    GROUP BY rd.d, h.concept_code
                ), members AS (
                    SELECT DISTINCT lu.d, s.member_date, s.concept_code, lu.stock_code
                    FROM lu
                    JOIN concept_snapshot_dates s ON s.d = lu.d
                    JOIN v_default_concept_stock_history h
                      ON CAST(h.trade_date AS DATE) = s.member_date
                     AND CAST(h.concept_code AS VARCHAR) = s.concept_code
                     AND regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '') = lu.stock_code
                ), kline AS (
                    SELECT CAST(trade_date AS DATE) AS d,
                           regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                           close, change_pct, turnover
                    FROM v_kline_daily
                    WHERE CAST(trade_date AS DATE) IN ({date_ph})
                ), daily_basic AS (
                    SELECT CAST(date AS DATE) AS d,
                           regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                           turnover_rate
                    FROM tushare_daily_basic
                    WHERE date IN ({date_ph})
                ), flow AS (
                    SELECT source_date AS d,
                           regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                           change_pct, main_net
                    FROM (
                        SELECT source_date,
                               regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                               change_pct, main_net,
                               row_number() OVER (
                                   PARTITION BY source_date,
                                       regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '')
                                   ORDER BY fetched_at DESC NULLS LAST
                               ) AS rn
                        FROM multi_source_stock_flow
                        WHERE source_date IN ({date_ph})
                          AND coalesce(is_stale, FALSE) = FALSE
                    ) ranked
                    WHERE rn = 1
                )
                 SELECT CAST(lu.d AS VARCHAR) AS trade_date,
                        m.concept_code, lu.stock_code, lu.stock_name,
                        lu.board_level, lu.limit_up_time,
                        kline.change_pct, kline.turnover, daily_basic.turnover_rate,
                        flow.main_net,
                        CAST(m.member_date AS VARCHAR) AS membership_date
                FROM lu
                JOIN members m ON m.d = lu.d AND m.stock_code = lu.stock_code
                 LEFT JOIN flow
                   ON flow.d = lu.d AND flow.stock_code = lu.stock_code
                 LEFT JOIN kline
                   ON kline.d = lu.d AND kline.stock_code = lu.stock_code
                 LEFT JOIN daily_basic
                   ON daily_basic.d = lu.d AND daily_basic.stock_code = lu.stock_code
                 """,
                 [*dates, *keep_list, *dates, *dates, *dates],
            )
        except Exception:
            stock_rows = []
        level_lookup = _derived_limit_board_levels(
            con,
            trade_date,
            [str(item.get("stock_code") or "") for item in stock_rows],
        )
        for item in stock_rows:
            key = (str(item.get("stock_code") or "")[:6], str(item.get("trade_date") or "")[:10])
            if key in level_lookup:
                item["board_level"] = level_lookup[key]

        window_by_sector: dict[str, dict[str, dict[str, Any]]] = {}
        for item in stock_rows:
            sector = by_code.get(str(item.get("concept_code") or ""))
            if not sector:
                continue
            day = str(item.get("trade_date") or "")[:10]
            cell = (sector.get("daily") or {}).get(day)
            if cell is None:
                continue
            stock_detail = {
                "stock_code": item.get("stock_code"),
                "stock_name": item.get("stock_name"),
                "board_level": item.get("board_level"),
                "limit_up_time": item.get("limit_up_time"),
                "pct_chg": item.get("change_pct"),
                "turnover": item.get("turnover_rate")
                if item.get("turnover_rate") is not None else item.get("turnover"),
                "amount": item.get("turnover"),
                "main_net": item.get("main_net"),
            }
            cell.setdefault("stocks", []).append(stock_detail)

            # Match akshare_skill's lazy detail contract: one stock is kept
            # when it was limit-up on any selected date, with a compact list
            # of its active dates for the window-level detail panel.
            concept_code = str(item.get("concept_code") or "")
            stock_code = str(item.get("stock_code") or "")
            sector_stocks = window_by_sector.setdefault(concept_code, {})
            summary = sector_stocks.setdefault(
                stock_code,
                {
                    "stock_code": stock_code,
                    "stock_name": item.get("stock_name"),
                    "limit_up_days": 0,
                    "limit_up_dates": [],
                    "latest_date": None,
                    "latest_pct_chg": None,
                    "max_board": None,
                },
            )
            seen_dates = summary.setdefault("_seen_dates", set())
            if day not in seen_dates:
                seen_dates.add(day)
                summary["limit_up_days"] = len(seen_dates)
                summary["limit_up_dates"].append(day)
            if summary["latest_date"] is None or day > summary["latest_date"]:
                summary["latest_date"] = day
                summary["latest_pct_chg"] = item.get("change_pct")
            board = item.get("board_level")
            if board is not None and (
                summary["max_board"] is None or float(board) > float(summary["max_board"])
            ):
                summary["max_board"] = board
        for sector in sectors:
            sector_window = window_by_sector.get(str(sector.get("id") or ""), {})
            for summary in sector_window.values():
                summary["limit_up_dates"] = sorted(summary.get("limit_up_dates") or [], reverse=True)
                summary.pop("latest_date", None)
                summary.pop("_seen_dates", None)
            sector["window_stocks"] = sorted(
                sector_window.values(),
                key=lambda row: (
                    -int(row.get("limit_up_days") or 0),
                    -(float(row.get("latest_pct_chg")) if row.get("latest_pct_chg") is not None else -999),
                    str(row.get("stock_code") or ""),
                ),
            )
            for cell in (sector.get("daily") or {}).values():
                stocks = cell.get("stocks") or []
                stocks.sort(
                    key=lambda row: (
                        -(float(row.get("board_level")) if row.get("board_level") is not None else -1),
                        -(float(row.get("pct_chg")) if row.get("pct_chg") is not None else -999),
                    )
                )
                cell["stock_rows"] = len(stocks)

    latest = dates[0] if dates else ""
    sectors.sort(
        key=lambda item: (
            -int((item.get("daily") or {}).get(latest, {}).get("limit_up") or 0),
            -float((item.get("daily") or {}).get(latest, {}).get("main_net") or 0),
            str(item.get("name") or ""),
        )
    )
    return {
        "status": "ready" if sectors else "degraded",
        "dates": dates,
        "sectors": sectors,
        "membership_date": str(member_date),
        "concept_source": "THS完整成分快照（ths_index_blockrank优先，TuShare THS成员补齐）",
        "top_per_day": top_per_day,
    }


def _period_label(day: str, kind: str) -> str:
    dt = datetime.strptime(str(day)[:10], "%Y-%m-%d")
    if kind == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if kind == "month":
        return f"{dt.year}-{dt.month:02d}"
    quarter = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{quarter}"


def _compound_pct(values: list[Any]) -> float | None:
    acc = 1.0
    count = 0
    for value in values:
        if value is None or value == "":
            continue
        try:
            acc *= 1.0 + float(value) / 100.0
        except (TypeError, ValueError):
            continue
        count += 1
    if count == 0:
        return None
    return round((acc - 1.0) * 100.0, 2)


def _period_frames(dates: list[str], kind: str, max_periods: int) -> list[dict[str, Any]]:
    """Group newest-first session dates into week/month/quarter frames."""
    frames: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for day in dates:
        label = _period_label(day, kind)
        if label not in index:
            if len(frames) >= max_periods:
                continue
            frame = {"label": label, "dates": []}
            index[label] = frame
            frames.append(frame)
        index[label]["dates"].append(day)
    for frame in frames:
        days = frame["dates"]
        frame["start"] = days[-1]
        frame["end"] = days[0]
        frame["trading_days"] = len(days)
    return frames


def _sector_period_review(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    lookback_days: int = 130,
) -> dict[str, Any]:
    """Week/month/quarter rollups matching akshare_skill PeriodReview.

    limit_up is the count of stock-day limit-up events in the window.
    pct_chg compounds the concept's daily change. main_net sums daily flow.
    A stock may appear under every concept it belongs to.
    """
    empty = {"week": {}, "month": {}, "quarter": {}, "status": "unavailable"}
    if not table_exists(con, "v_limit_pool") or not table_exists(con, "v_default_concept_stock_history"):
        return empty
    try:
        date_rows = con.execute(
            """
            SELECT d FROM (
                SELECT DISTINCT CAST(trade_date AS DATE) AS d
                FROM v_limit_pool
                WHERE CAST(trade_date AS DATE) <= CAST(? AS DATE)
            ) t
            ORDER BY d DESC
            LIMIT ?
            """,
            [trade_date, lookback_days],
        ).fetchall()
        dates = [str(row[0]) for row in date_rows]
        if not dates:
            return empty
        broad = list(_BROAD_TRAIL_CONCEPTS)
        broad_ph = ",".join("?" for _ in broad)
        date_ph = ",".join("?" for _ in dates)
        if table_exists(con, "multi_source_sector_flow"):
            flow_sql = f"""
                flow AS (
                    SELECT CAST(source_date AS DATE) AS d,
                           CAST(sector_code AS VARCHAR) AS sector_code,
                           change_pct, main_net
                    FROM (
                        SELECT source_date, sector_code, change_pct, main_net,
                               row_number() OVER (
                                   PARTITION BY source_date, sector_code
                                   ORDER BY fetched_at DESC NULLS LAST
                               ) AS rn
                        FROM multi_source_sector_flow
                        WHERE source_date IN ({date_ph})
                          AND coalesce(is_stale, FALSE) = FALSE
                          AND sector_type IN ('ths_concept', 'ths_concept_derived')
                    ) ranked WHERE rn = 1
                )
            """
        else:
            flow_sql = """
                flow AS (
                    SELECT CAST(NULL AS DATE) AS d,
                           CAST(NULL AS VARCHAR) AS sector_code,
                           CAST(NULL AS DOUBLE) AS change_pct,
                           CAST(NULL AS DOUBLE) AS main_net
                    WHERE FALSE
                )
            """
        rows = _fetch_dicts(
            con,
            f"""
            WITH lu AS (
                SELECT CAST(trade_date AS DATE) AS d,
                       regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code
                FROM v_limit_pool
                WHERE CAST(trade_date AS DATE) IN ({date_ph})
                GROUP BY 1, 2
            ), requested_dates AS (
                SELECT DISTINCT d FROM lu
            ), concept_snapshot_dates AS (
                SELECT rd.d, CAST(h.concept_code AS VARCHAR) AS concept_code,
                       max(CAST(h.trade_date AS DATE)) AS member_date
                FROM requested_dates rd
                JOIN v_default_concept_stock_history h
                  ON CAST(h.trade_date AS DATE) <= rd.d
                 AND h.concept_code LIKE 'THS-%'
                 AND coalesce(h.concept_name, '') NOT IN ({broad_ph})
                GROUP BY rd.d, h.concept_code
            ), member_asof AS (
                SELECT s.d, s.member_date, s.concept_code,
                       max(h.concept_name) AS concept_name,
                       regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '') AS stock_code
                FROM concept_snapshot_dates s
                JOIN v_default_concept_stock_history h
                  ON CAST(h.trade_date AS DATE) = s.member_date
                 AND CAST(h.concept_code AS VARCHAR) = s.concept_code
                GROUP BY s.d, s.member_date, s.concept_code,
                         regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '')
            ), sizes AS (
                SELECT d, concept_code, member_date,
                       count(DISTINCT stock_code) AS member_count
                FROM member_asof
                GROUP BY d, concept_code, member_date
            ), counted AS (
                SELECT m.d, m.concept_code, max(m.concept_name) AS concept_name,
                       m.member_date, z.member_count,
                       count(DISTINCT lu.stock_code) AS limit_up_count
                FROM member_asof m
                JOIN sizes z
                  ON z.d = m.d AND z.concept_code = m.concept_code
                  AND z.member_date = m.member_date
                JOIN lu ON lu.d = m.d AND lu.stock_code = m.stock_code
                GROUP BY m.d, m.concept_code, m.member_date, z.member_count
            ), {flow_sql}, counted_rows AS (
                SELECT c.d, c.concept_code, c.concept_name, c.member_date,
                       c.limit_up_count, c.member_count, f.change_pct, f.main_net
                FROM counted c
                LEFT JOIN flow f
                  ON f.d = c.d AND f.sector_code = c.concept_code
            )
            SELECT CAST(d AS VARCHAR) AS trade_date,
                   concept_code, concept_name, limit_up_count,
                   member_count,
                   CAST(member_date AS VARCHAR) AS membership_date,
                   change_pct, main_net
            FROM counted_rows
            """,
            [*dates, *broad, *dates] if table_exists(con, "multi_source_sector_flow") else [*dates, *broad],
        )
    except Exception:
        return empty

    member_dates = [str(row.get("membership_date") or "")[:10] for row in rows if row.get("membership_date")]
    member_date = max(member_dates) if member_dates else None
    if not member_date:
        return empty

    by_concept: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("concept_code") or "")
        day = str(row.get("trade_date") or "")[:10]
        if not code or not day:
            continue
        item = by_concept.setdefault(code, {"id": code, "name": row.get("concept_name") or code, "days": {}})
        item["days"][day] = {
            "limit_up": int(row.get("limit_up_count") or 0),
            "member_count": int(row.get("member_count") or 0),
            "pct_chg": row.get("change_pct"),
            "main_net": row.get("main_net"),
        }

    specs = (("week", 8), ("month", 6), ("quarter", 4))
    top_per = 0
    bundle: dict[str, Any] = {
        "status": "ready",
        "membership_date": str(member_date),
        "concept_source": "THS完整成分快照（ths_index_blockrank优先，TuShare THS成员补齐）",
    }
    keep: set[str] = set()
    for kind, max_periods in specs:
        frames = _period_frames(dates, kind, max_periods)
        sectors: list[dict[str, Any]] = []
        for code, item in by_concept.items():
            periods: dict[str, Any] = {}
            for frame in frames:
                daily = [item["days"][d] for d in frame["dates"] if d in item["days"]]
                if not daily:
                    continue
                periods[frame["label"]] = {
                    "limit_up": sum(int(cell.get("limit_up") or 0) for cell in daily),
                    "member_count": int(daily[0].get("member_count") or 0),
                    "pct_chg": _compound_pct([cell.get("pct_chg") for cell in daily]),
                    "main_net": sum(float(cell.get("main_net") or 0) for cell in daily) or None,
                    "stocks": [],
                }
            if periods:
                sectors.append({"id": code, "name": item["name"], "periods": periods})
        latest = frames[0]["label"] if frames else ""
        sectors.sort(
            key=lambda sector: (
                -int((sector.get("periods") or {}).get(latest, {}).get("limit_up") or 0),
                -float((sector.get("periods") or {}).get(latest, {}).get("main_net") or 0),
            )
        )
        for frame in frames:
            ranked = sorted(
                sectors,
                key=lambda sector: -int((sector.get("periods") or {}).get(frame["label"], {}).get("limit_up") or 0),
            )
            selected = ranked if not top_per or top_per < 1 else ranked[:top_per]
            for sector in selected:
                if frame["label"] in (sector.get("periods") or {}):
                    keep.add(sector["id"])
        bundle[kind] = {
            "periods": [
                {
                    "label": frame["label"],
                    "start": frame["start"],
                    "end": frame["end"],
                    "trading_days": frame["trading_days"],
                }
                for frame in frames
            ],
            "sectors": [sector for sector in sectors if sector["id"] in keep],
        }

    if keep and table_exists(con, "multi_source_stock_flow"):
        keep_list = sorted(keep)
        keep_ph = ",".join("?" for _ in keep_list)
        date_ph = ",".join("?" for _ in dates)
        try:
            stock_rows = _fetch_dicts(
                con,
                f"""
                WITH lu AS (
                    SELECT CAST(trade_date AS DATE) AS d,
                           regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                           max(stock_name) AS stock_name,
                           max(board_level) AS board_level
                    FROM v_limit_pool
                    WHERE CAST(trade_date AS DATE) IN ({date_ph})
                    GROUP BY 1, 2
                ), requested_dates AS (
                    SELECT DISTINCT d FROM lu
                ), concept_snapshot_dates AS (
                    SELECT rd.d, CAST(h.concept_code AS VARCHAR) AS concept_code,
                           max(CAST(h.trade_date AS DATE)) AS member_date
                    FROM requested_dates rd
                    JOIN v_default_concept_stock_history h
                      ON CAST(h.trade_date AS DATE) <= rd.d
                     AND h.concept_code IN ({keep_ph})
                    GROUP BY rd.d, h.concept_code
                ), members AS (
                    SELECT DISTINCT lu.d, s.member_date, s.concept_code, lu.stock_code
                    FROM lu
                    JOIN concept_snapshot_dates s ON s.d = lu.d
                    JOIN v_default_concept_stock_history h
                      ON CAST(h.trade_date AS DATE) = s.member_date
                     AND CAST(h.concept_code AS VARCHAR) = s.concept_code
                     AND regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '') = lu.stock_code
                ), flow AS (
                    SELECT source_date AS d, stock_code, change_pct
                    FROM (
                        SELECT source_date,
                               regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                               change_pct,
                               row_number() OVER (
                                   PARTITION BY source_date,
                                       regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '')
                                   ORDER BY fetched_at DESC NULLS LAST
                               ) AS rn
                        FROM multi_source_stock_flow
                        WHERE source_date IN ({date_ph})
                          AND coalesce(is_stale, FALSE) = FALSE
                    ) ranked WHERE rn = 1
                )
                SELECT CAST(lu.d AS VARCHAR) AS trade_date, m.concept_code,
                       lu.stock_code, lu.stock_name, lu.board_level, flow.change_pct,
                       CAST(m.member_date AS VARCHAR) AS membership_date
                FROM lu
                JOIN members m ON m.d = lu.d AND m.stock_code = lu.stock_code
                LEFT JOIN flow ON flow.d = lu.d AND flow.stock_code = lu.stock_code
                """,
                [*dates, *keep_list, *dates],
            )
        except Exception:
            stock_rows = []
        level_lookup = _derived_limit_board_levels(
            con,
            trade_date,
            [str(item.get("stock_code") or "") for item in stock_rows],
        )
        for item in stock_rows:
            key = (str(item.get("stock_code") or "")[:6], str(item.get("trade_date") or "")[:10])
            if key in level_lookup:
                item["board_level"] = level_lookup[key]

        day_to_kind_label: dict[str, dict[str, str]] = {day: {} for day in dates}
        for kind, _max in specs:
            for frame in _period_frames(dates, kind, _max):
                for day in frame["dates"]:
                    day_to_kind_label[day][kind] = frame["label"]
        stock_acc: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in stock_rows:
            code = str(row.get("concept_code") or "")
            day = str(row.get("trade_date") or "")[:10]
            stock = str(row.get("stock_code") or "")
            if not code or not day or not stock:
                continue
            for kind in ("week", "month", "quarter"):
                label = day_to_kind_label.get(day, {}).get(kind)
                if not label:
                    continue
                key = (kind, code, label, stock)
                item = stock_acc.get(key)
                if item is None:
                    item = {
                        "stock_code": stock,
                        "stock_name": row.get("stock_name") or stock,
                        "limit_up": 0,
                        "pcts": [],
                        "max_board": None,
                    }
                    stock_acc[key] = item
                seen_dates = item.setdefault("_limit_up_dates", set())
                if day in seen_dates:
                    continue
                seen_dates.add(day)
                item["limit_up"] = len(seen_dates)
                if row.get("change_pct") is not None:
                    item["pcts"].append(row.get("change_pct"))
                board = row.get("board_level")
                if board is not None and (item["max_board"] is None or float(board) > float(item["max_board"])):
                    item["max_board"] = board

        # Period stock performance must use the first/last close in the
        # window.  Compounding only the days on which a stock hit limit-up
        # creates a false "period return" and was the main divergence from
        # akshare_skill's PeriodStockPanel.
        if stock_acc and table_exists(con, "v_kline_daily"):
            stock_codes = sorted({str(item["stock_code"]) for item in stock_acc.values()})
            stock_ph = ",".join("?" for _ in stock_codes)
            valid_period_labels = {
                kind: {
                    frame["label"]
                    for frame in _period_frames(dates, kind, max_periods)
                }
                for kind, max_periods in specs
            }
            try:
                kline_rows = _fetch_dicts(
                    con,
                    f"""
                    SELECT CAST(trade_date AS VARCHAR) AS trade_date,
                           regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                           close
                    FROM (
                        SELECT trade_date, stock_code, close,
                               row_number() OVER (
                                   PARTITION BY CAST(trade_date AS DATE),
                                       regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '')
                                   ORDER BY coalesce(is_fallback, FALSE), fetched_at DESC NULLS LAST
                               ) AS rn
                        FROM v_kline_daily
                        WHERE CAST(trade_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                          AND regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') IN ({stock_ph})
                          AND close IS NOT NULL AND close > 0
                    ) ranked
                    WHERE rn = 1
                    ORDER BY trade_date, stock_code
                    """,
                    [min(dates), max(dates), *stock_codes],
                )
            except Exception:
                kline_rows = []
            keys_by_lookup: dict[tuple[str, str, str], list[tuple[str, str, str, str]]] = {}
            for key in stock_acc:
                keys_by_lookup.setdefault((key[0], key[2], key[3]), []).append(key)
            for row in kline_rows:
                stock = str(row.get("stock_code") or "")
                day = str(row.get("trade_date") or "")[:10]
                if not stock or not day:
                    continue
                for kind in ("week", "month", "quarter"):
                    label = _period_label(day, kind)
                    if label not in valid_period_labels.get(kind, set()):
                        continue
                    for key in keys_by_lookup.get((kind, label, stock), []):
                        close = row.get("close")
                        if close is not None:
                            stock_acc[key].setdefault("closes", []).append((day, float(close)))
        for item in stock_acc.values():
            closes = sorted(item.get("closes") or [], key=lambda value: value[0])
            if len(closes) >= 2 and closes[0][1] > 0:
                item["period_return_pct"] = round((closes[-1][1] / closes[0][1] - 1.0) * 100.0, 2)
            else:
                item["period_return_pct"] = None
        by_kind_sector: dict[str, dict[str, dict]] = {
            kind: {sector["id"]: sector for sector in bundle[kind]["sectors"]}
            for kind, _ in specs
        }
        for (kind, code, label, _stock), item in stock_acc.items():
            sector = by_kind_sector.get(kind, {}).get(code)
            if not sector:
                continue
            cell = (sector.get("periods") or {}).get(label)
            if not cell:
                continue
            item.pop("_limit_up_dates", None)
            cell.setdefault("stocks", []).append(
                {
                    "stock_code": item["stock_code"],
                    "stock_name": item["stock_name"],
                    "limit_up": item["limit_up"],
                    "pct_chg": item.get("period_return_pct"),
                    "flow_pct_chg": _compound_pct(item["pcts"]),
                    "board_level": item["max_board"],
                }
            )
        for kind, _ in specs:
            for sector in bundle[kind]["sectors"]:
                for cell in (sector.get("periods") or {}).values():
                    stocks = cell.get("stocks") or []
                    stocks.sort(
                        key=lambda row: (
                            -int(row.get("limit_up") or 0),
                            -(float(row.get("pct_chg")) if row.get("pct_chg") is not None else -999),
                        )
                    )
                    cell["stocks"] = stocks
    return bundle


