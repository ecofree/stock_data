"""Daily operator review report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_columns, table_exists
from trade_system.readiness import assess_trade_date_readiness
from trade_system.reports.real_data_backfill import build_real_data_backfill_status
from trade_system.review_statistics import build_daily_review_statistics


def _fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _rows(con: duckdb.DuckDBPyConnection, table: str, sql: str, params: list[Any]) -> list[dict]:
    if not table_exists(con, table):
        return []
    try:
        return _fetch_dicts(con, sql, params)
    except Exception:
        return []


def _fmt_money(value: Any) -> str:
    """Render canonical yuan amounts compactly without losing sign."""
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 100_000_000:
        return f"{sign}{number / 100_000_000:.2f}亿"
    if number >= 10_000:
        return f"{sign}{number / 10_000:.2f}万"
    return f"{sign}{number:.0f}"


def _fmt_pct(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _format_flow_rows(rows: list[dict], money_fields: tuple[str, ...], pct_fields: tuple[str, ...] = ("change_pct",)) -> list[dict]:
    formatted = []
    for row in rows:
        item = dict(row)
        for field in money_fields:
            if field in item:
                item[field] = _fmt_money(item[field])
        for field in pct_fields:
            if field in item:
                item[field] = _fmt_pct(item[field])
        formatted.append(item)
    return formatted


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
        "stock_flow_persistence": [], "sector_flow_persistence": [],
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

    # Persistence is deliberately calculated from the canonical flow table,
    # not from the provider-specific top-50 snapshot.  This prevents a stock
    # from looking persistent merely because one provider repeated it.
    if table_exists(con, "multi_source_stock_flow") and result["stock_inflow"]:
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

    if table_exists(con, "multi_source_sector_flow") and result["sector_inflow"]:
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
        "tushare": [], "kline": [], "ths": {}, "outcomes": 0, "qlib": [], "strategy": [],
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
    if table_exists(con, "qlib_shadow_evaluation"):
        cols = set(table_columns(con, "qlib_shadow_evaluation"))
        qlib_select = ["model_id", "'shadow' AS stage", "sample_count", "hit_rate"]
        qlib_select.append("top_quantile_return AS avg_forward_return_pct" if "top_quantile_return" in cols else "NULL AS avg_forward_return_pct")
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


def build_daily_review_context(db_path: str | Path, trade_date: str | None = None) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        selected_date = trade_date or _latest_date(con)
        regime_order = "generated_at DESC NULLS LAST" if "generated_at" in table_columns(con, "market_regime_snapshot") else "trade_date DESC"
        regime = _rows(
            con,
            "market_regime_snapshot",
            f"SELECT * FROM market_regime_snapshot WHERE trade_date = ? ORDER BY {regime_order} LIMIT 1",
            [selected_date],
        )
        sectors = _rows(
            con,
            "sector_rotation_score",
            "SELECT * FROM sector_rotation_score WHERE trade_date = ? ORDER BY score DESC LIMIT 10",
            [selected_date],
        )
        stages = _rows(
            con,
            "stock_candidate_stage_signal",
            """
            SELECT stage, stock_code, stock_name, score, decision
            FROM stock_candidate_stage_signal
            WHERE trade_date = ?
            ORDER BY stage, score DESC NULLS LAST, stock_code
            LIMIT 80
            """,
            [selected_date],
        )
        alerts = _rows(
            con,
            "alert_events",
            "SELECT severity, category, message FROM alert_events WHERE trade_date = ? ORDER BY severity, category",
            [selected_date],
        )
        watchlist = _rows(
            con,
            "watchlist",
            "SELECT stock_code, stock_name, sector_code, thesis, invalidation, priority, status FROM watchlist WHERE trade_date = ? ORDER BY priority",
            [selected_date],
        )
        plans = _rows(
            con,
            "trade_plan",
            "SELECT stock_code, stock_name, setup_type, max_position_pct, status, entry_condition, stop_condition FROM trade_plan WHERE trade_date = ? ORDER BY max_position_pct DESC",
            [selected_date],
        )
        risk = _rows(
            con,
            "risk_snapshot",
            "SELECT risk_state, total_position_pct, max_single_position_pct, max_sector_position_pct, evidence_json FROM risk_snapshot WHERE trade_date = ? ORDER BY created_at DESC LIMIT 1",
            [selected_date],
        )
        journal = _rows(
            con,
            "trade_journal",
            "SELECT stock_code, stock_name, action, action_time, reason, mistake_tag FROM trade_journal WHERE trade_date = ? ORDER BY action_time, stock_code",
            [selected_date],
        )
        outcomes = _rows(
            con,
            "operator_trade_outcome",
            """
            SELECT stock_code, stock_name, execution_status, position_pct, gross_return_pct,
                   net_return_pct, outcome_tag, mistake_tag, review_note
            FROM operator_trade_outcome
            WHERE trade_date = ?
            ORDER BY stock_code
            """,
            [selected_date],
        )
        capital_flow = _capital_flow_review(con, selected_date)
        market_context = _market_context_review(con, selected_date)
        data_sources = _data_source_review(con, selected_date)
        try:
            readiness = assess_trade_date_readiness(con, selected_date, stage="postmarket")
        except Exception as exc:
            readiness = {
                "trade_date": selected_date, "stage": "postmarket",
                "ready": False, "analytics_ready": False, "execution_ready": False,
                "missing_groups": [f"readiness_error:{type(exc).__name__}"],
                "groups": [], "actionable_candidates": 0,
                "tradable_candidates": 0, "risk_approved_candidates": 0,
                "executable_candidates": 0,
            }
    finally:
        con.close()

    suggested = regime[0].get("suggested_position_pct") if regime else None
    risk_position = risk[0].get("total_position_pct") if risk else None
    analytics_ready = bool(readiness.get("analytics_ready"))
    execution_ready = bool(readiness.get("execution_ready"))
    effective_position = suggested if analytics_ready else 0

    return {
        "trade_date": selected_date,
        "regime": regime[0] if regime else {},
        "sectors": sectors,
        "stages": stages,
        "alerts": alerts,
        "watchlist": watchlist,
        "plans": plans,
        "risk": risk[0] if risk else {},
        "journal": journal,
        "outcomes": outcomes,
        "capital_flow": capital_flow,
        "market_context": market_context,
        "data_sources": data_sources,
        "readiness": readiness,
        "execution_control": {
            "analytics_ready": analytics_ready,
            "execution_ready": execution_ready,
            "effective_position_pct": effective_position,
            "suggested_position_pct": suggested,
            "risk_position_pct": risk_position,
            "override": "ALLOW_REVIEW_ONLY" if analytics_ready and not execution_ready else "ALLOW" if execution_ready else "BLOCK",
        },
        "statistics": build_daily_review_statistics(db_path),
        "backfill": build_real_data_backfill_status(db_path),
    }


def _table_rows(rows: list[dict], columns: list[str], empty_text: str = "No rows") -> list[str]:
    if not rows:
        return [f"| {empty_text} |" + " |" * (len(columns) - 1)]
    output = []
    for row in rows:
        output.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return output


def _render_flow_review_sections(flow: dict[str, Any]) -> list[str]:
    """Render the explicit fund-flow and research-pick contract.

    These sections deliberately distinguish research candidates from executable
    orders.  A daily review must still be useful when one provider is stale or
    a required execution gate (K-line, sector, auction, or outcome) is absent.
    """
    stock_meta = flow.get("stock_flow_meta", {})
    sector_meta = flow.get("sector_flow_meta", {})
    sector_title = "THS Concept" if "ths" in str(sector_meta.get("taxonomy") or "").lower() else "Sector"
    sector_status = sector_meta.get("batch_status") or "unknown"
    sector_coverage = sector_meta.get("batch_coverage_pct")
    lines = [
        "## Capital Flow Coverage",
        "",
        f"- Stock flow: `{stock_meta.get('codes', 0)}` codes / `{stock_meta.get('rows', 0)}` rows; providers `{stock_meta.get('providers', '')}`; batch `{stock_meta.get('batch_status', 'unknown')}` ({stock_meta.get('batch_coverage_pct', '')}%); fetched `{stock_meta.get('fetched_at', '')}`.",
        f"- Sector flow: `{sector_meta.get('codes', 0)}` concepts / `{sector_meta.get('rows', 0)}` rows; taxonomy `{sector_meta.get('taxonomy', '')}`; batch `{sector_status}` ({sector_coverage if sector_coverage is not None else ''}%); fetched `{sector_meta.get('fetched_at', '')}`.",
        "",
        "### Individual Stock Main-Net Inflow Top 50",
        "",
        "| Code | Name | Main Net | Super | Large | Change % | Provider |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    stock_money = ("main_net", "super_net", "large_net")
    sector_money = ("main_net",)
    lines.extend(_table_rows(_format_flow_rows(flow.get("stock_inflow", []), stock_money), ["stock_code", "stock_name", "main_net", "super_net", "large_net", "change_pct", "provider"], "No stock inflow rows"))
    lines.extend(
        [
            "",
            "### Individual Stock Main-Net Outflow Top 50",
            "",
            "| Code | Name | Main Net | Super | Large | Change % | Provider |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("stock_outflow", []), stock_money), ["stock_code", "stock_name", "main_net", "super_net", "large_net", "change_pct", "provider"], "No stock outflow rows"))
    lines.extend(
        [
            "",
            f"### THS Concept Flow Inflow Top 10 ({sector_title} rows)",
            "",
            "| Concept | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("sector_inflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], f"No {sector_title.lower()} inflow rows"))
    lines.extend(
        [
            "",
            f"### THS Concept Flow Outflow Top 10 ({sector_title} rows)",
            "",
            "| Concept | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("sector_outflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], f"No {sector_title.lower()} outflow rows"))
    lines.extend(
        [
            "",
            "### Eastmoney Industry Flow Inflow Top 10",
            "",
            "| Industry | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("industry_inflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], "No industry inflow rows"))
    lines.extend(
        [
            "",
            "### Eastmoney Industry Flow Outflow Top 10",
            "",
            "| Industry | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("industry_outflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], "No industry outflow rows"))
    lines.extend(
        [
            "",
            "### THS Concepts With Limit-Up Stocks",
            "",
            "| Concept | Limit-Up Count | Stocks |",
            "|---|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("sector_limit_up", []), ["sector_name", "limit_up_count", "limit_up_stocks"], "No concept limit-up mapping rows"))
    lines.extend(
        [
            "",
            "### Potential Stocks (Research Only)",
            "",
            "These are review candidates, not executable orders. Promote only after K-line, sector, auction, readiness, and manual-risk gates pass.",
            "",
            "| Code | Name | Score | Main Net | Flow Rank | Selection Status |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("candidate_picks", []), ["stock_code", "stock_name", "score", "main_net", "flow_rank", "selection_status"], "No research candidates"))
    lines.extend(
        [
            "",
            "### Capital Flow Persistence (20-Day Evidence)",
            "",
            "| Stock | Observed Days | Positive Days | 20-Day Main Net | Latest |",
            "|---|---:|---:|---:|---|",
        ]
    )
    persistence = [
        {**row, "twenty_day_main_net": _fmt_money(row.get("twenty_day_main_net"))}
        for row in flow.get("stock_flow_persistence", [])
    ]
    lines.extend(_table_rows(persistence, ["stock_code", "observed_days", "positive_days", "twenty_day_main_net", "latest_date"], "No stock persistence rows"))
    lines.extend(
        [
            "",
            "### THS Concept Persistence (20-Day Evidence)",
            "",
            "| Concept | Observed Days | Positive Days | 20-Day Main Net | Latest |",
            "|---|---:|---:|---:|---|",
        ]
    )
    sector_persistence = [
        {**row, "twenty_day_main_net": _fmt_money(row.get("twenty_day_main_net"))}
        for row in flow.get("sector_flow_persistence", [])
    ]
    lines.extend(_table_rows(sector_persistence, ["sector_name", "observed_days", "positive_days", "twenty_day_main_net", "latest_date"], "No concept persistence rows"))
    lines.extend(
        [
            "",
            "### 龙虎榜 (Post-Market Evidence)",
            "",
            "| Stock | Change | Reason | Buy | Sell | Net |",
            "|---|---:|---|---:|---:|---:|",
        ]
    )
    lhb_rows = [
        {**row, "buy_amount": _fmt_money(row.get("buy_amount")),
         "sell_amount": _fmt_money(row.get("sell_amount")),
         "net_amount": _fmt_money(row.get("net_amount"))}
        for row in flow.get("lhb", [])
    ]
    lines.extend(_table_rows(lhb_rows, ["stock_name", "change_pct", "reason", "buy_amount", "sell_amount", "net_amount"], "No LHB rows for this date"))
    return lines


def render_daily_review_markdown(context: dict) -> str:
    regime = context.get("regime", {})
    risk = context.get("risk", {})
    stats = context.get("statistics", {})
    backfill = context.get("backfill", {})
    flow = context.get("capital_flow", {})
    readiness = context.get("readiness", {})
    control = context.get("execution_control", {})
    market_context = context.get("market_context", {})
    data_sources = context.get("data_sources", {})
    stock_meta = flow.get("stock_flow_meta", {})
    sector_meta = flow.get("sector_flow_meta", {})
    lines = [
        f"# Daily Review - {context.get('trade_date', '')}",
        "",
        "## Market Regime",
        "",
        f"- Regime: `{regime.get('regime', 'unknown')}`",
        f"- Regime score: `{regime.get('regime_score', '')}`",
        f"- Suggested position: `{regime.get('suggested_position_pct', '')}%`",
        f"- Risk state: `{risk.get('risk_state', 'unknown')}`",
        "",
        "## P0 Data And Execution Gate",
        "",
        f"- Analytics ready: `{str(readiness.get('analytics_ready', False)).lower()}`; execution ready: `{str(readiness.get('execution_ready', False)).lower()}`.",
        f"- Control: `{control.get('override', 'BLOCK')}`; effective position cap: `{control.get('effective_position_pct', 0)}%`.",
        f"- Actionable / tradable / risk-approved / executable: `{readiness.get('actionable_candidates', 0)} / {readiness.get('tradable_candidates', 0)} / {readiness.get('risk_approved_candidates', 0)} / {readiness.get('executable_candidates', 0)}`.",
        f"- Missing groups: `{', '.join(readiness.get('missing_groups', [])) or 'none'}`.",
        "- Review basis: same-date postmarket snapshot; use the live freshness gate before any executable decision.",
    ]
    lines.extend(
        [
            "",
            "## Market Breadth And Auction Context",
            "",
            "| Evidence | Values |",
            "|---|---|",
        ]
    )
    breadth_rows = market_context.get("breadth", [])
    if breadth_rows:
        for row in breadth_rows:
            values = ", ".join(f"{key}={value}" for key, value in row.items() if value not in (None, ""))
            lines.append(f"| breadth | {values} |")
    else:
        lines.append("| breadth | no same-date breadth snapshot |")
    for row in market_context.get("limit_summary", []):
        values = ", ".join(f"{key}={value}" for key, value in row.items() if value not in (None, ""))
        lines.append(f"| limit-up/down | {values} |")
    for row in market_context.get("auction", []):
        values = ", ".join(f"{key}={value}" for key, value in row.items() if value not in (None, ""))
        lines.append(f"| auction anomaly | {values} |")
    lhb_summary = market_context.get("lhb_summary") or {}
    lines.append(f"| LHB | rows={lhb_summary.get('rows', 0)}, stocks={lhb_summary.get('stocks', 0)}, fetched={lhb_summary.get('fetched_at') or '-'} |")
    lines.extend(["", "## Provider And Research Checkpoints", "", "| Dataset | Status / Evidence |", "|---|---|"])
    tushare_rows = data_sources.get("tushare", [])
    if tushare_rows:
        for row in tushare_rows:
            detail = f"status={row.get('status')}, rows={row.get('rows_written', 0)}, attempts={row.get('attempts', 0)}, error={row.get('last_error') or '-'}"
            lines.append(f"| {row.get('dataset')} | {detail} |")
    else:
        lines.append("| TuShare checkpoints | no same-date checkpoint |")
    for row in data_sources.get("kline", []):
        lines.append(f"| {row.get('relation')} | latest={row.get('latest') or '-'}, same-date rows={row.get('same_date_rows', 0)} |")
    ths = data_sources.get("ths") or {}
    if ths:
        lines.append(
            f"| THS membership | snapshot={ths.get('trade_date')}, raw={ths.get('concepts', 0)} concepts/{ths.get('members', 0)} members, "
            f"usable={ths.get('usable_concepts', 0)} concepts/{ths.get('usable_members', 0)} members, "
            f"checkpoint={ths.get('success', 0)}/{ths.get('checkpoint_rows', 0)}, partial/stale={ths.get('partial', 0)} |"
        )
    lines.append(f"| Operator outcomes | same-date rows={data_sources.get('outcomes', 0)}; zero means proxy/backtest only |")
    for row in data_sources.get("qlib", [])[:5]:
        lines.append(f"| QLib shadow {row.get('model_id') or '-'} | stage={row.get('stage')}, samples={row.get('sample_count', 0)}, hit={row.get('hit_rate')}, avg={row.get('avg_forward_return_pct')}, impact={row.get('signal_impact')} |")
    if not data_sources.get("qlib"):
        lines.append("| QLib shadow | no evaluation rows; optional dependency/model signal is not evidence for execution |")
    for row in data_sources.get("strategy", [])[:5]:
        lines.append(f"| Strategy {row.get('strategy_id') or '-'} | stage={row.get('stage')}, samples={row.get('sample_count', 0)}, win={row.get('win_rate')}, avg={row.get('avg_return_pct')}, verdict={row.get('verdict')} |")
    if not data_sources.get("strategy"):
        lines.append("| Strategy backtest | no stored summary rows |")
    lines.extend([""] + _render_flow_review_sections(flow))
    lines.extend(["", "## Mainline Themes", "", "| Sector | Score | Strength | Limit Up |", "|---|---:|---:|---:|"])
    lines.extend(_table_rows(context.get("sectors", []), ["sector_name", "score", "strength_value", "limit_up_count"]))

    lines.extend(
        [
            "",
            "## Four-Stage Candidates",
            "",
            "| Stage | Stock | Score | Decision |",
            "|---|---|---:|---|",
        ]
    )
    stage_rows = [
        {
            "stage": row.get("stage"),
            "stock": row.get("stock_name") or row.get("stock_code"),
            "score": row.get("score"),
            "decision": row.get("decision"),
        }
        for row in context.get("stages", [])
    ]
    lines.extend(_table_rows(stage_rows, ["stage", "stock", "score", "decision"]))

    lines.extend(["", "## Risk Alerts", "", "| Severity | Category | Message |", "|---|---|---|"])
    lines.extend(_table_rows(context.get("alerts", []), ["severity", "category", "message"]))

    lines.extend(["", "## Plan Execution", "", "| Stock | Max Position | Status | Entry | Stop |", "|---|---:|---|---|---|"])
    plan_rows = [
        {
            "stock": row.get("stock_name") or row.get("stock_code"),
            "max_position_pct": row.get("max_position_pct"),
            "status": row.get("status"),
            "entry_condition": row.get("entry_condition"),
            "stop_condition": row.get("stop_condition"),
        }
        for row in context.get("plans", [])
    ]
    lines.extend(_table_rows(plan_rows, ["stock", "max_position_pct", "status", "entry_condition", "stop_condition"]))

    lines.extend(["", "## Mistakes And Invalidations", "", "| Stock | Action | Reason | Tag |", "|---|---|---|---|"])
    review_rows = [
        {
            "stock": row.get("stock_name") or row.get("stock_code"),
            "action": row.get("action"),
            "reason": row.get("reason"),
            "mistake_tag": row.get("mistake_tag"),
        }
        for row in context.get("journal", [])
        if row.get("mistake_tag") not in ("", None, "none")
    ]
    lines.extend(_table_rows(review_rows, ["stock", "action", "reason", "mistake_tag"]))

    lines.extend(
        [
            "",
            "## Outcome Review",
            "",
            "| Stock | Status | Position | Gross % | Net % | Outcome | Mistake | Note |",
            "|---|---|---:|---:|---:|---|---|---|",
        ]
    )
    outcome_rows = [
        {
            "stock": row.get("stock_name") or row.get("stock_code"),
            "execution_status": row.get("execution_status"),
            "position_pct": row.get("position_pct"),
            "gross_return_pct": row.get("gross_return_pct"),
            "net_return_pct": row.get("net_return_pct"),
            "outcome_tag": row.get("outcome_tag"),
            "mistake_tag": row.get("mistake_tag"),
            "review_note": row.get("review_note"),
        }
        for row in context.get("outcomes", [])
    ]
    lines.extend(
        _table_rows(
            outcome_rows,
            [
                "stock",
                "execution_status",
                "position_pct",
                "gross_return_pct",
                "net_return_pct",
                "outcome_tag",
                "mistake_tag",
                "review_note",
            ],
            "No imported operator outcomes",
        )
    )

    lines.extend(
        [
            "",
            "## Next-Day Focus",
            "",
            "- Recheck top mainline themes before auction.",
            "- Keep candidates only if auction and intraday evidence confirm the thesis.",
            "- Respect risk_snapshot max position before any manual action.",
            "",
            "## Data Gaps And Degradation",
            "",
        ]
    )
    for gap in flow.get("coverage_alerts", []):
        lines.append(f"- {gap}")
    if not readiness.get("analytics_ready", False):
        lines.append("- P0: same-date evidence is incomplete; this report is review-only and no position should be opened from it.")
    gaps = backfill.get("gaps", [])
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps[:20])
    else:
        lines.append("- No critical gaps detected.")

    lines.extend(
        [
            "",
            "## Stage Validation",
            "",
            "| Stage | Return Samples | Verdict |",
            "|---|---:|---|",
        ]
    )
    for stage, item in sorted(stats.get("stage_statistics", {}).items()):
        lines.append(f"| {stage} | {item.get('return_sample_count', 0)} | `{item.get('verdict')}` |")
    lines.append("")
    return "\n".join(lines)


def write_daily_review(db_path: str | Path, out_path: str | Path, trade_date: str | None = None) -> Path:
    context = build_daily_review_context(db_path, trade_date)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_daily_review_markdown(context), encoding="utf-8")
    return path
