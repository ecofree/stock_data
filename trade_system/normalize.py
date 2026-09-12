"""Build normalized, deduplicated DuckDB views for trading workflows."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.schema import _refresh_default_concept_views
from trade_system.logging_setup import get_logger
from trade_system.quality import table_columns, table_exists
from trade_system.limit_rules import limit_threshold_sql
from trade_system.source_authority import provider_rank
from trade_system.units import normalization_sql

logger = get_logger(__name__)


def _empty_view_sql(view_name: str, columns: list[tuple[str, str]]) -> str:
    fields = ", ".join(f"CAST(NULL AS {dtype}) AS {name}" for name, dtype in columns)
    return f"CREATE OR REPLACE VIEW {view_name} AS SELECT {fields} WHERE false"


def _timestamp_column(con: duckdb.DuckDBPyConnection, table_name: str) -> str:
    cols = table_columns(con, table_name)
    for candidate in ("fetched_at", "updated_at"):
        if candidate in cols:
            return candidate
    return "current_timestamp"


def _relation_has_rows(con: duckdb.DuckDBPyConnection, relation_name: str) -> bool:
    try:
        row = con.execute(f'SELECT count(*) FROM "{relation_name}"').fetchone()
    except Exception as exc:
        # Missing optional relations are routine control flow here, so this
        # stays at debug level; schema drift still shows up with KPL_LOG_LEVEL=DEBUG.
        logger.debug("relation %s not queryable: %s", relation_name, exc)
        return False
    return bool(row and row[0] > 0)


def _table_has_columns(con: duckdb.DuckDBPyConnection, table_name: str, columns: list[str]) -> bool:
    existing = set(table_columns(con, table_name))
    return bool(existing) and all(column in existing for column in columns)


def _empty_relation_sql(columns: list[tuple[str, str]]) -> str:
    fields = ", ".join(f"CAST(NULL AS {dtype}) AS {name}" for name, dtype in columns)
    return f"SELECT {fields} WHERE false"


def _latest_cte(table_name: str, partition_cols: list[str], order_col: str) -> str:
    partition = ", ".join(partition_cols)
    if order_col == "current_timestamp":
        order = "current_timestamp DESC"
    else:
        order = f"{order_col} DESC NULLS LAST"
    return (
        f"SELECT * FROM ("
        f"SELECT *, row_number() OVER (PARTITION BY {partition} ORDER BY {order}) AS _rn "
        f"FROM {table_name}"
        f") WHERE _rn = 1"
    )


def _canonical_kline_cte(table_name: str, order_col: str, value_column: str = "close") -> str:
    """Return one usable row per stock/date/normalized K-line period.

    Historical collectors wrote both ``D`` and ``d``.  Treating the raw value
    as part of the business key allowed two daily rows into normalized views,
    which in turn made backtests mistake the second same-day row for T+1.
    """
    if order_col == "current_timestamp":
        order = "rowid DESC"
    else:
        order = f"{order_col} DESC NULLS LAST, rowid DESC"
    return (
        "SELECT * FROM ("
        "SELECT *, row_number() OVER ("
        "PARTITION BY date, stock_code, upper(coalesce(nullif(trim(ktype), ''), 'D')) "
        f"ORDER BY {order}) AS _rn "
        f"FROM {table_name} "
        f"WHERE {value_column} IS NOT NULL "
        "AND upper(coalesce(nullif(trim(ktype), ''), 'D')) = 'D'"
        ") WHERE _rn = 1"
    )


def _create_market_daily(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("limit_up_count", "INTEGER"),
        ("limit_down_count", "INTEGER"),
        ("rise_count", "INTEGER"),
        ("fall_count", "INTEGER"),
        ("consecutive_count", "INTEGER"),
        ("fetched_at", "TIMESTAMP"),
        ("market_daily_source", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
    ]
    has_summary = table_exists(con, "daily_summary")
    has_tushare = table_exists(con, "tushare_daily")
    if not has_summary and not has_tushare:
        con.execute(_empty_view_sql("v_market_daily", cols))
        return
    sources = []
    if has_summary:
        order_col = _timestamp_column(con, "daily_summary")
        fetched_expr = order_col if order_col != "current_timestamp" else "current_timestamp"
        summary_fallback = (
            "lower(coalesce(source_kind,'')) IN "
            "('fallback','derived','derived_current')"
            if "source_kind" in set(table_columns(con, "daily_summary"))
            else "false"
        )
        sources.append(
            f"""
            SELECT CAST(date AS VARCHAR) AS trade_date,
                   limit_up_count, limit_down_count, rise_count, fall_count,
                   consecutive_count, {fetched_expr} AS fetched_at,
                   'daily_summary' AS market_daily_source,
                   {summary_fallback} AS is_fallback,
                   1 AS source_priority
            FROM ({_latest_cte("daily_summary", ["date"], order_col)})
            """
        )
    if has_tushare:
        has_basic = table_exists(con, "tushare_stock_basic")
        basic_join = (
            "LEFT JOIN tushare_stock_basic b ON b.stock_code=d.stock_code"
            if has_basic else ""
        )
        threshold = limit_threshold_sql("d.stock_code", "b.stock_name", "b.market") if has_basic else "9.8"
        if table_exists(con, "eastmoney_limit_up_pool"):
            limit_up_expr = (
                "coalesce((SELECT count(DISTINCT e.stock_code) FROM eastmoney_limit_up_pool e "
                "WHERE e.date=d.date), sum(CASE WHEN d.change_pct >= " + threshold + " THEN 1 ELSE 0 END))"
            )
            consecutive_expr = (
                "coalesce((SELECT count(DISTINCT e.stock_code) FROM eastmoney_limit_up_pool e "
                "WHERE e.date=d.date AND e.board_level>=2), 0)"
            )
            derived_source = "tushare_daily+eastmoney_limit_up_pool"
        else:
            limit_up_expr = "sum(CASE WHEN d.change_pct >= " + threshold + " THEN 1 ELSE 0 END)"
            consecutive_expr = "0"
            derived_source = "tushare_daily_threshold_fallback"
        sources.append(
            f"""
            SELECT CAST(d.date AS VARCHAR) AS trade_date,
                   CAST({limit_up_expr} AS INTEGER) AS limit_up_count,
                   CAST(sum(CASE WHEN d.change_pct <= -({threshold}) THEN 1 ELSE 0 END) AS INTEGER) AS limit_down_count,
                   CAST(sum(CASE WHEN d.change_pct>0 THEN 1 ELSE 0 END) AS INTEGER) AS rise_count,
                   CAST(sum(CASE WHEN d.change_pct<0 THEN 1 ELSE 0 END) AS INTEGER) AS fall_count,
                   CAST({consecutive_expr} AS INTEGER) AS consecutive_count,
                   max(d.fetched_at) AS fetched_at,
                   '{derived_source}' AS market_daily_source,
                   true AS is_fallback,
                   2 AS source_priority
            FROM tushare_daily d
            {basic_join}
            GROUP BY d.date
            """
        )
    combined = " UNION ALL ".join(sources)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_market_daily AS
        SELECT trade_date, limit_up_count, limit_down_count, rise_count,
               fall_count, consecutive_count, fetched_at, market_daily_source,
               is_fallback
        FROM ({combined})
        QUALIFY row_number() OVER (
            PARTITION BY trade_date ORDER BY source_priority, fetched_at DESC NULLS LAST
        )=1
        """
    )


def _create_sector_daily(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("sector_code", "VARCHAR"),
        ("sector_name", "VARCHAR"),
        ("sector_type", "VARCHAR"),
        ("strength_value", "DOUBLE"),
        ("limit_up_count", "INTEGER"),
        ("seal_rate", "DOUBLE"),
        ("up_count", "INTEGER"),
        ("down_count", "INTEGER"),
        ("stock_count", "INTEGER"),
    ]
    if not table_exists(con, "sector_strength"):
        con.execute(_empty_view_sql("v_sector_daily", cols))
        return
    strength_latest = _latest_cte("sector_strength", ["date", "sector_code"], _timestamp_column(con, "sector_strength"))
    if table_exists(con, "sector_ranking"):
        ranking_latest = _latest_cte("sector_ranking", ["date", "sector_code"], _timestamp_column(con, "sector_ranking"))
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_sector_daily AS
            SELECT
                CAST(s.date AS VARCHAR) AS trade_date,
                s.sector_code,
                coalesce(r.sector_name, '') AS sector_name,
                s.strength_value,
                s.zhangting AS limit_up_count,
                s.fengban_rate AS seal_rate,
                s.up_count,
                s.down_count,
                r.stock_count
            FROM ({strength_latest}) s
            LEFT JOIN ({ranking_latest}) r
              ON s.date = r.date AND s.sector_code = r.sector_code
            """
        )
    else:
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_sector_daily AS
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                sector_code,
                '' AS sector_name,
                strength_value,
                zhangting AS limit_up_count,
                fengban_rate AS seal_rate,
                up_count,
                down_count,
                CAST(NULL AS INTEGER) AS stock_count
            FROM ({strength_latest})
            """
        )


def _create_limit_pool(con: duckdb.DuckDBPyConnection) -> None:
    hithink_priority = provider_rank("limit_pool", "hithink")
    xiaodefa_priority = provider_rank("limit_pool", "xiaodefa")
    eastmoney_priority = provider_rank("limit_pool", "eastmoney")
    kpl_priority = provider_rank("limit_pool", "kpl")
    cols = [
        ("trade_date", "VARCHAR"),
        ("board_level", "INTEGER"),
        ("stock_code", "VARCHAR"),
        ("stock_name", "VARCHAR"),
        ("limit_up_time", "VARCHAR"),
        ("fetched_at", "TIMESTAMP"),
        ("source", "VARCHAR"),
        ("source_priority", "INTEGER"),
    ]
    has_l2 = table_exists(con, "l2_realtime_all_boards")
    has_ladder = table_exists(con, "ladder_realtime_boards")
    has_eastmoney = table_exists(con, "eastmoney_limit_up_pool")
    has_tushare = table_exists(con, "official_limit_pool")
    has_xdf = (
        table_exists(con, "xdf_limit_pool")
        and _relation_has_rows(con, "xdf_limit_pool")
    )
    if has_l2 or has_ladder or has_eastmoney or has_tushare or has_xdf:
        sources = []
        if has_tushare:
            # Priority 0: backfilled exchange-grade history with exact
            # limit_times (board level) and first/last seal times.  Days
            # covered by the backfill use this source exclusively.
            sources.append(
                f"""
                SELECT CAST(trade_date AS VARCHAR) AS trade_date,
                       continue_day_cnt AS board_level, stock_code, stock_name,
                       limit_up_time, fetched_at,
                       coalesce(source, 'hithink') AS source,
                       {hithink_priority} AS source_priority
                FROM official_limit_pool
                WHERE continue_day_cnt IS NOT NULL
                """
            )
        if has_xdf:
            # Priority 0 as well: TuShare limit_list_d is exchange-grade too.
            # Same-priority rows coexist within a day so the two official
            # sources fill each other's gaps instead of excluding one another;
            # the QUALIFY dedupe resolves per-stock conflicts by freshness.
            # board_level is derived at collection time from local kline streaks.
            sources.append(
                f"""
                SELECT CAST(trade_date AS VARCHAR) AS trade_date,
                       TRY_CAST(board_level AS INTEGER) AS board_level,
                       regexp_replace(CAST(ts_code AS VARCHAR), '[.].*$', '') AS stock_code,
                       max(name) AS stock_name,
                       CAST(NULL AS VARCHAR) AS limit_up_time,
                       CAST(max(fetched_at) AS TIMESTAMP) AS fetched_at,
                       'xiaodefa' AS source,
                       {xiaodefa_priority} AS source_priority
                FROM xdf_limit_pool
                GROUP BY 1, 2, 3
                """
            )
        if has_eastmoney:
            latest = _latest_cte(
                "eastmoney_limit_up_pool",
                ["date", "stock_code"],
                _timestamp_column(con, "eastmoney_limit_up_pool"),
            )
            sources.append(
                f"""
                SELECT CAST(date AS VARCHAR) AS trade_date,
                       board_level, stock_code, stock_name, limit_up_time, fetched_at,
                       'eastmoney' AS source,
                       {eastmoney_priority} AS source_priority
                FROM ({latest})
                """
            )
        if has_l2:
            latest = _latest_cte(
                "l2_realtime_all_boards",
                ["date", "stock_code"],
                _timestamp_column(con, "l2_realtime_all_boards"),
            )
            sources.append(
                f"""
                SELECT CAST(date AS VARCHAR) AS trade_date,
                       board_level, stock_code, stock_name, limit_up_time, fetched_at,
                       'kpl_l2' AS source,
                       {kpl_priority} AS source_priority
                FROM ({latest})
                """
            )
        if has_ladder:
            latest = _latest_cte(
                "ladder_realtime_boards",
                ["date", "stock_code"],
                _timestamp_column(con, "ladder_realtime_boards"),
            )
            sources.append(
                f"""
                SELECT CAST(date AS VARCHAR) AS trade_date,
                       TRY_CAST(board_type AS INTEGER) AS board_level,
                       stock_code, stock_name, limit_up_time, fetched_at,
                       'kpl_ladder' AS source,
                       {kpl_priority} AS source_priority
                FROM ({latest})
                """
            )
        combined = " UNION ALL ".join(sources)
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_limit_pool AS
            SELECT trade_date, board_level, stock_code, stock_name, limit_up_time, fetched_at,
                   source, source_priority
            FROM (
                SELECT * FROM ({combined})
            )
            QUALIFY row_number() OVER (
                PARTITION BY trade_date, stock_code ORDER BY source_priority, fetched_at DESC NULLS LAST
            ) = 1
            """
        )
        return
    con.execute(_empty_view_sql("v_limit_pool", cols))


def _create_limit_pool_rich(con: duckdb.DuckDBPyConnection) -> None:
    """Exchange-grade limit-up detail (seal times, reopen count, float mv).

    Empty-tolerant: when the backfill table is absent the view collapses to
    the same shape fed only by v_limit_pool basics.
    """
    if not table_exists(con, "official_limit_pool"):
        con.execute(
            _empty_view_sql(
                "v_limit_pool_rich",
                [
                    ("trade_date", "DATE"),
                    ("stock_code", "VARCHAR"),
                    ("stock_name", "VARCHAR"),
                    ("industry", "VARCHAR"),
                    ("board_level", "INTEGER"),
                    ("close", "DOUBLE"),
                    ("pct_chg", "DOUBLE"),
                    ("fd_amount", "DOUBLE"),
                    ("first_time", "VARCHAR"),
                    ("last_time", "VARCHAR"),
                    ("open_times", "INTEGER"),
                ],
            )
        )
        return
    con.execute(
        """
        CREATE OR REPLACE VIEW v_limit_pool_rich AS
        SELECT CAST(trade_date AS DATE) AS trade_date, stock_code, stock_name,
               limit_up_reason AS industry, continue_day_cnt AS board_level,
               close, pct_chg, seal_money AS fd_amount,
               limit_up_time AS first_time, NULL AS last_time,
               NULL AS open_times
        FROM official_limit_pool
        """
    )


def _create_lhb_daily(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("stock_code", "VARCHAR"),
        ("stock_name", "VARCHAR"),
        ("reason", "VARCHAR"),
        ("buy_amount", "BIGINT"),
        ("sell_amount", "BIGINT"),
        ("net_amount", "BIGINT"),
    ]
    if not table_exists(con, "lhb_list"):
        con.execute(_empty_view_sql("v_lhb_daily", cols))
        return
    latest = _latest_cte("lhb_list", ["date", "stock_code", "reason"], _timestamp_column(con, "lhb_list"))
    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_lhb_daily AS
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            stock_code,
            stock_name,
            reason,
            buy_amount,
            sell_amount,
            net_amount
        FROM ({latest})
        """
    )


def _create_stock_pool(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("sector_code", "VARCHAR"),
        ("stock_code", "VARCHAR"),
        ("stock_name", "VARCHAR"),
        ("change_pct", "DOUBLE"),
        ("turnover", "BIGINT"),
        ("market_cap", "BIGINT"),
    ]
    if not table_exists(con, "sector_stocks"):
        con.execute(_empty_view_sql("v_stock_pool", cols))
        return
    latest = _latest_cte(
        "sector_stocks",
        ["date", "sector_code", "stock_code"],
        _timestamp_column(con, "sector_stocks"),
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_stock_pool AS
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            sector_code,
            stock_code,
            stock_name,
            change_pct,
            turnover,
            market_cap
        FROM ({latest})
        """
    )


def _create_auction_status(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("stock_code", "VARCHAR"),
        ("stock_name", "VARCHAR"),
        ("auction_amount", "BIGINT"),
        ("anomaly_type", "VARCHAR"),
        ("anomaly_value", "DOUBLE"),
        ("auction_strength", "DOUBLE"),
        ("confirmation", "VARCHAR"),
        ("source_table", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
        ("fetched_at", "TIMESTAMP"),
    ]
    sources = []
    if _relation_has_rows(con, "auction_tick"):
        latest = _latest_cte(
            "auction_tick",
            ["date", "stock_code", "time"],
            _timestamp_column(con, "auction_tick"),
        )
        tick_columns = set(table_columns(con, "auction_tick"))
        tick_unit = "volume_unit" if "volume_unit" in tick_columns else "'unknown'"
        tick_volume_shares = (
            f"CASE lower(coalesce(nullif({tick_unit}, ''), 'unknown')) "
            "WHEN 'hands' THEN volume * 100 WHEN 'shares' THEN volume ELSE NULL END"
        )
        sources.append(
            f"""
            SELECT CAST(date AS VARCHAR) AS trade_date,stock_code,
                   CAST(NULL AS VARCHAR) AS stock_name,
                   CASE WHEN count(DISTINCT lower(coalesce(nullif({tick_unit}, ''), 'unknown'))) = 1
                        AND lower(max(coalesce(nullif({tick_unit}, ''), 'unknown'))) = 'hands'
                        THEN CAST(sum(price * volume * 100) AS BIGINT)
                        WHEN count(DISTINCT lower(coalesce(nullif({tick_unit}, ''), 'unknown'))) = 1
                        AND lower(max(coalesce(nullif({tick_unit}, ''), 'unknown'))) = 'shares'
                        THEN CAST(sum(price * volume) AS BIGINT)
                        ELSE CAST(NULL AS BIGINT) END AS auction_amount,
                   'tick_volume' AS anomaly_type,
                   CAST(sum({tick_volume_shares}) AS DOUBLE) AS anomaly_value,
                   round(sum({tick_volume_shares}) / 1000000.0, 2) AS auction_strength,
                   'tick_confirmed' AS confirmation,'auction_tick' AS source_table,
                   false AS is_fallback,max(fetched_at) AS fetched_at,1 AS source_priority
            FROM ({latest}) GROUP BY date,stock_code
            """
        )
    if _relation_has_rows(con, "auction_quote_snapshot"):
        latest = _latest_cte(
            "auction_quote_snapshot",
            ["date", "stock_code", "quote_time", "provider"],
            _timestamp_column(con, "auction_quote_snapshot"),
        )
        quote_columns = set(table_columns(con, "auction_quote_snapshot"))
        quote_unit = "volume_unit" if "volume_unit" in quote_columns else "'unknown'"
        quote_volume_shares = (
            f"CASE lower(coalesce(nullif({quote_unit}, ''), 'unknown')) "
            "WHEN 'hands' THEN cumulative_volume * 100 "
            "WHEN 'shares' THEN cumulative_volume ELSE NULL END"
        )
        sources.append(
            f"""
            SELECT CAST(date AS VARCHAR) AS trade_date,stock_code,
                   CAST(NULL AS VARCHAR) AS stock_name,
                   CASE WHEN count(DISTINCT lower(coalesce(nullif({quote_unit}, ''), 'unknown'))) = 1
                        AND lower(max(coalesce(nullif({quote_unit}, ''), 'unknown'))) = 'hands'
                        THEN CAST(max(indicative_price) * max(cumulative_volume) * 100 AS BIGINT)
                        WHEN count(DISTINCT lower(coalesce(nullif({quote_unit}, ''), 'unknown'))) = 1
                        AND lower(max(coalesce(nullif({quote_unit}, ''), 'unknown'))) = 'shares'
                        THEN CAST(max(indicative_price) * max(cumulative_volume) AS BIGINT)
                        ELSE CAST(NULL AS BIGINT) END AS auction_amount,
                   'order_book_imbalance' AS anomaly_type,
                   arg_max(order_imbalance,fetched_at) AS anomaly_value,
                   round(coalesce(arg_max(order_imbalance,fetched_at), 0) * 100
                         + least(coalesce(max({quote_volume_shares}), 0) / 100000.0,20),2) AS auction_strength,
                   'quote_confirmed' AS confirmation,
                   'auction_quote_snapshot' AS source_table,
                   false AS is_fallback,max(fetched_at) AS fetched_at,2 AS source_priority
            FROM ({latest}) GROUP BY date,stock_code
            """
        )
    if _relation_has_rows(con, "auction_bidding_anomaly"):
        latest = _latest_cte(
            "auction_bidding_anomaly",
            ["date", "stock_code", "anomaly_type"],
            _timestamp_column(con, "auction_bidding_anomaly"),
        )
        sources.append(
            f"""
            SELECT CAST(date AS VARCHAR) AS trade_date,stock_code,
                   CAST(NULL AS VARCHAR) AS stock_name,
                   CAST(NULL AS BIGINT) AS auction_amount,
                   anomaly_type,anomaly_value,anomaly_value AS auction_strength,
                   CASE WHEN anomaly_value > 0 THEN 'confirmed' ELSE 'watch' END AS confirmation,
                   'auction_bidding_anomaly' AS source_table,false AS is_fallback,
                   fetched_at,3 AS source_priority
            FROM ({latest})
            """
        )
    if _relation_has_rows(con, "advanced_morning_bidding_summary"):
        latest = _latest_cte(
            "advanced_morning_bidding_summary",
            ["date"],
            _timestamp_column(con, "advanced_morning_bidding_summary"),
        )
        sources.append(
            f"""
            SELECT CAST(date AS VARCHAR) AS trade_date,
                   CAST(NULL AS VARCHAR) AS stock_code,
                   CAST(NULL AS VARCHAR) AS stock_name,total_amount AS auction_amount,
                   'summary_fallback' AS anomaly_type,
                   CAST(limit_up_count-limit_down_count AS DOUBLE) AS anomaly_value,
                   round(coalesce(total_amount,0)/100000000.0
                         +coalesce(limit_up_count,0)*1.5-coalesce(limit_down_count,0),2) AS auction_strength,
                   CASE WHEN limit_up_count>limit_down_count THEN 'market_confirmed'
                        WHEN limit_up_count=limit_down_count THEN 'neutral' ELSE 'weak' END AS confirmation,
                   'advanced_morning_bidding_summary' AS source_table,true AS is_fallback,
                   fetched_at,4 AS source_priority
            FROM ({latest})
            """
        )
    if _relation_has_rows(con, "advanced_morning_bidding_list"):
        latest = _latest_cte(
            "advanced_morning_bidding_list",
            ["date", "stock_code"],
            _timestamp_column(con, "advanced_morning_bidding_list"),
        )
        sources.append(
            f"""
            SELECT CAST(date AS VARCHAR) AS trade_date,stock_code,stock_name,
                   bidding_amount AS auction_amount,'stock_amount_fallback' AS anomaly_type,
                   CAST(bidding_amount AS DOUBLE) AS anomaly_value,
                   round(coalesce(bidding_amount,0)/100000000.0,2) AS auction_strength,
                   'stock_amount_only' AS confirmation,
                   'advanced_morning_bidding_list' AS source_table,true AS is_fallback,
                   fetched_at,5 AS source_priority
            FROM ({latest})
            """
        )
    if not sources:
        con.execute(_empty_view_sql("v_auction_status", cols))
        return
    combined = " UNION ALL ".join(sources)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_auction_status AS
        SELECT trade_date,stock_code,stock_name,auction_amount,anomaly_type,
               anomaly_value,auction_strength,confirmation,source_table,
               is_fallback,fetched_at
        FROM (
          SELECT *,row_number() OVER (
            PARTITION BY trade_date,coalesce(stock_code,'__MARKET__')
            ORDER BY source_priority,fetched_at DESC NULLS LAST
          ) AS _rn
          FROM ({combined})
        )
        WHERE _rn=1
        """
    )


def _create_sector_capital(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("sector_code", "VARCHAR"),
        ("sector_name", "VARCHAR"),
        ("sector_type", "VARCHAR"),
        ("main_net_inflow", "BIGINT"),
        ("super_net_inflow", "BIGINT"),
        ("big_net_inflow", "BIGINT"),
        ("mid_net_inflow", "BIGINT"),
        ("small_net_inflow", "BIGINT"),
        ("strength_value", "DOUBLE"),
        ("limit_up_count", "INTEGER"),
        ("seal_rate", "DOUBLE"),
        ("source_table", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
        ("fetched_at", "TIMESTAMP"),
    ]
    # Prefer the current multi-source flow snapshot when it exists.  The
    # legacy ``sector_capital`` table is an older 801.* taxonomy and often has
    # blank names; using it for a newer trading date silently produces an
    # anonymous, stale rotation list.
    if _relation_has_rows(con, "multi_source_sector_flow"):
        con.execute(
            """
            CREATE OR REPLACE VIEW v_sector_capital AS
            WITH ranked AS (
                SELECT
                    CAST(source_date AS VARCHAR) AS trade_date,
                    sector_code,
                    coalesce(nullif(sector_name, ''), sector_code) AS sector_name,
                    coalesce(nullif(sector_type, ''), CASE WHEN sector_code LIKE 'THS-%' THEN 'ths_concept' ELSE 'em_industry' END) AS sector_type,
                    main_net AS main_net_inflow,
                    super_net AS super_net_inflow,
                    large_net AS big_net_inflow,
                    mid_net AS mid_net_inflow,
                    small_net AS small_net_inflow,
                    change_pct AS strength_value,
                    CAST(NULL AS INTEGER) AS limit_up_count,
                    CAST(NULL AS DOUBLE) AS seal_rate,
                    'multi_source_sector_flow' AS source_table,
                    false AS is_fallback,
                    fetched_at,
                    row_number() OVER (
                        PARTITION BY source_date, sector_code
                        ORDER BY CASE WHEN sector_type = 'ths_concept_derived' THEN 0 ELSE 1 END,
                                 fetched_at DESC NULLS LAST
                    ) AS rn
                FROM multi_source_sector_flow
                WHERE coalesce(is_stale, false) = false
            )
            SELECT trade_date, sector_code, sector_name, sector_type, main_net_inflow,
                   super_net_inflow, big_net_inflow, mid_net_inflow,
                   small_net_inflow, strength_value, limit_up_count, seal_rate,
                   source_table, is_fallback, fetched_at
            FROM ranked WHERE rn = 1
            """
        )
        return
    if _relation_has_rows(con, "sector_capital"):
        latest = _latest_cte(
            "sector_capital",
            ["date", "sector_code"],
            _timestamp_column(con, "sector_capital"),
        )
        fetched_expr = "c.fetched_at" if "fetched_at" in table_columns(con, "sector_capital") else "current_timestamp"
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_sector_capital AS
            SELECT
                CAST(c.date AS VARCHAR) AS trade_date,
                c.sector_code,
                coalesce(s.sector_name, '') AS sector_name,
                CASE WHEN c.sector_code LIKE 'THS-%' THEN 'ths_concept' ELSE 'em_industry' END AS sector_type,
                c.main_net_inflow,
                c.super_net_inflow,
                c.big_net_inflow,
                c.mid_net_inflow,
                c.small_net_inflow,
                s.strength_value,
                s.limit_up_count,
                s.seal_rate,
                'sector_capital' AS source_table,
                false AS is_fallback,
                {fetched_expr} AS fetched_at
            FROM ({latest}) c
            LEFT JOIN v_sector_daily s
              ON CAST(c.date AS VARCHAR) = s.trade_date AND c.sector_code = s.sector_code
            """
        )
        return
    if _relation_has_rows(con, "v_sector_daily"):
        con.execute(
            """
            CREATE OR REPLACE VIEW v_sector_capital AS
            SELECT
                trade_date,
                sector_code,
                sector_name,
                CASE WHEN sector_code LIKE 'THS-%' THEN 'ths_concept' ELSE 'em_industry' END AS sector_type,
                CAST(NULL AS BIGINT) AS main_net_inflow,
                CAST(NULL AS BIGINT) AS super_net_inflow,
                CAST(NULL AS BIGINT) AS big_net_inflow,
                CAST(NULL AS BIGINT) AS mid_net_inflow,
                CAST(NULL AS BIGINT) AS small_net_inflow,
                strength_value,
                limit_up_count,
                seal_rate,
                'sector_strength' AS source_table,
                true AS is_fallback,
                CAST(NULL AS TIMESTAMP) AS fetched_at
            FROM v_sector_daily
            """
        )
        return
    con.execute(_empty_view_sql("v_sector_capital", cols))


def _create_kline_daily(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("stock_code", "VARCHAR"),
        ("open", "DOUBLE"),
        ("high", "DOUBLE"),
        ("low", "DOUBLE"),
        ("close", "DOUBLE"),
        ("volume", "BIGINT"),
        ("turnover", "BIGINT"),
        ("change_pct", "DOUBLE"),
        ("ktype", "VARCHAR"),
        ("source_table", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
        ("fetched_at", "TIMESTAMP"),
        ("volume_unit", "VARCHAR"),
        ("amount_unit", "VARCHAR"),
        ("adjustment", "VARCHAR"),
        ("provider", "VARCHAR"),
    ]
    # TuShare is the project's bulk daily-history source.  Prefer it for
    # dates it actually covers, then retain newer/auxiliary rows from the
    # legacy KPL views instead of letting the legacy table hide fresh history.
    if _relation_has_rows(con, "tushare_daily"):
        tushare_order = _timestamp_column(con, "tushare_daily")
        tushare_latest = (
            "SELECT * FROM (SELECT *, row_number() OVER ("
            f"PARTITION BY date, stock_code ORDER BY {tushare_order} DESC NULLS LAST, rowid DESC"
            ") AS _rn FROM tushare_daily WHERE close IS NOT NULL) WHERE _rn=1"
        )
        tushare_columns = set(table_columns(con, "tushare_daily"))
        tushare_volume_unit = "volume_unit" if "volume_unit" in tushare_columns else "'unknown'"
        tushare_amount_unit = "amount_unit" if "amount_unit" in tushare_columns else "'unknown'"
        tushare_adjustment = "adjustment" if "adjustment" in tushare_columns else "'unknown'"
        tushare_provider = "provider" if "provider" in tushare_columns else "'unknown'"
        tushare_sql = """
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                open,
                high,
                low,
                close,
                {canonical_volume} AS volume,
                {canonical_amount} AS turnover,
                change_pct,
                'D' AS ktype,
                'tushare_daily' AS source_table,
                false AS is_fallback,
                fetched_at,
                'shares' AS volume_unit,
                'yuan' AS amount_unit,
                coalesce(nullif({tushare_adjustment}, ''), 'none') AS adjustment,
                coalesce(nullif({tushare_provider}, ''), 'unknown') AS provider
            FROM ({tushare_latest})
            WHERE stock_code IS NOT NULL AND date IS NOT NULL
        """.format(
            tushare_latest=tushare_latest,
            tushare_volume_unit=tushare_volume_unit,
            tushare_amount_unit=tushare_amount_unit,
            tushare_adjustment=tushare_adjustment,
            tushare_provider=tushare_provider,
            canonical_volume=normalization_sql('volume', tushare_volume_unit, 'volume'),
            canonical_amount=normalization_sql('turnover', tushare_amount_unit, 'amount'),
        )
        fallback_sql = None
        if _relation_has_rows(con, "kline"):
            latest = _canonical_kline_cte("kline", _timestamp_column(con, "kline"))
            kline_columns = set(table_columns(con, "kline"))
            volume_unit = "volume_unit" if "volume_unit" in kline_columns else "'unknown'"
            amount_unit = "amount_unit" if "amount_unit" in kline_columns else "'unknown'"
            adjustment = "adjustment" if "adjustment" in kline_columns else "'unknown'"
            provider = "provider" if "provider" in kline_columns else "'unknown'"
            fallback_sql = f"""
                SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, open, high,
                       low, close,
                       {normalization_sql('volume', volume_unit, 'volume')} AS volume,
                       {normalization_sql('turnover', amount_unit, 'amount')} AS turnover,
                       change_pct,
                       upper(coalesce(nullif(trim(ktype), ''), 'D')) AS ktype,
                       'kline' AS source_table, false AS is_fallback, fetched_at,
                       'shares' AS volume_unit, 'yuan' AS amount_unit,
                       coalesce(nullif({adjustment}, ''), 'unknown') AS adjustment,
                       coalesce(nullif({provider}, ''), 'unknown') AS provider
                FROM ({latest})
            """
        elif _relation_has_rows(con, "advanced_kline_today"):
            latest = _canonical_kline_cte(
                "advanced_kline_today", _timestamp_column(con, "advanced_kline_today")
            )
            fallback_sql = f"""
                SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, open, high,
                       low, close, CAST(NULL AS BIGINT) AS volume, CAST(NULL AS BIGINT) AS turnover,
                       CAST(NULL AS DOUBLE) AS change_pct,
                       upper(coalesce(nullif(trim(ktype), ''), 'D')) AS ktype,
                       'advanced_kline_today' AS source_table, true AS is_fallback, fetched_at,
                       'shares' AS volume_unit, 'yuan' AS amount_unit,
                       'unknown' AS adjustment, 'advanced_kline_today' AS provider
                FROM ({latest})
            """
        if fallback_sql:
            con.execute(
                f"""
                CREATE OR REPLACE VIEW v_kline_daily AS
                WITH tushare_rows AS ({tushare_sql}), fallback_rows AS ({fallback_sql})
                SELECT * FROM tushare_rows
                UNION ALL
                SELECT f.* FROM fallback_rows f
                WHERE NOT EXISTS (
                    SELECT 1 FROM tushare_rows t
                    WHERE t.trade_date=f.trade_date AND t.stock_code=f.stock_code
                )
                """
            )
        else:
            con.execute(f"CREATE OR REPLACE VIEW v_kline_daily AS SELECT * FROM ({tushare_sql})")
        return
    if _relation_has_rows(con, "kline"):
        latest = _canonical_kline_cte("kline", _timestamp_column(con, "kline"))
        kline_columns = set(table_columns(con, "kline"))
        volume_unit = "volume_unit" if "volume_unit" in kline_columns else "'unknown'"
        amount_unit = "amount_unit" if "amount_unit" in kline_columns else "'unknown'"
        adjustment = "adjustment" if "adjustment" in kline_columns else "'unknown'"
        provider = "provider" if "provider" in kline_columns else "'unknown'"
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_kline_daily AS
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                open,
                high,
                low,
                close,
                {normalization_sql('volume', volume_unit, 'volume')} AS volume,
                {normalization_sql('turnover', amount_unit, 'amount')} AS turnover,
                change_pct,
                upper(coalesce(nullif(trim(ktype), ''), 'D')) AS ktype,
                'kline' AS source_table,
                false AS is_fallback,
                fetched_at,
                'shares' AS volume_unit,
                'yuan' AS amount_unit,
                coalesce(nullif({adjustment}, ''), 'unknown') AS adjustment,
                coalesce(nullif({provider}, ''), 'unknown') AS provider
            FROM ({latest})
            """
        )
        return
    if _relation_has_rows(con, "advanced_kline_today"):
        latest = _canonical_kline_cte(
            "advanced_kline_today", _timestamp_column(con, "advanced_kline_today")
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_kline_daily AS
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                open,
                high,
                low,
                close,
                CAST(NULL AS BIGINT) AS volume,
                CAST(NULL AS BIGINT) AS turnover,
                CAST(NULL AS DOUBLE) AS change_pct,
                upper(coalesce(nullif(trim(ktype), ''), 'D')) AS ktype,
                'advanced_kline_today' AS source_table,
                true AS is_fallback,
                fetched_at,
                'shares' AS volume_unit,
                'yuan' AS amount_unit,
                'unknown' AS adjustment,
                'advanced_kline_today' AS provider
            FROM ({latest})
            """
        )
        return
    if _relation_has_rows(con, "advanced_gujia_kline"):
        latest = _canonical_kline_cte(
            "advanced_gujia_kline",
            _timestamp_column(con, "advanced_gujia_kline"),
            value_column="gujia_value",
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_kline_daily AS
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                CAST(NULL AS DOUBLE) AS open,
                CAST(NULL AS DOUBLE) AS high,
                CAST(NULL AS DOUBLE) AS low,
                gujia_value AS close,
                CAST(NULL AS BIGINT) AS volume,
                CAST(NULL AS BIGINT) AS turnover,
                CAST(NULL AS DOUBLE) AS change_pct,
                upper(coalesce(nullif(trim(ktype), ''), 'D')) AS ktype,
                'advanced_gujia_kline' AS source_table,
                true AS is_fallback,
                fetched_at,
                'shares' AS volume_unit,
                'yuan' AS amount_unit,
                'unknown' AS adjustment,
                'advanced_gujia_kline' AS provider
            FROM ({latest})
            """
        )
        return
    con.execute(_empty_view_sql("v_kline_daily", cols))


def _latest_or_empty(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    partition_cols: list[str],
    columns: list[tuple[str, str]],
) -> str:
    if _relation_has_rows(con, table_name):
        return _latest_cte(table_name, partition_cols, _timestamp_column(con, table_name))
    return _empty_relation_sql(columns)


def _create_market_state_inputs(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("limit_up_count", "INTEGER"),
        ("limit_down_count", "INTEGER"),
        ("rise_count", "INTEGER"),
        ("fall_count", "INTEGER"),
        ("consecutive_count", "INTEGER"),
        ("broken_limit_up_count", "INTEGER"),
        ("blown_limit_up_count", "INTEGER"),
        ("blown_limit_up_rate", "DOUBLE"),
        ("cgl", "DOUBLE"),
        ("yll", "DOUBLE"),
        ("success_rate", "DOUBLE"),
        ("new_high_count", "INTEGER"),
        ("earning_effect_score", "DOUBLE"),
        ("acute_drop_risk_score", "DOUBLE"),
        ("source_table", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
        ("fetched_at", "TIMESTAMP"),
    ]
    if not _relation_has_rows(con, "v_market_daily"):
        con.execute(_empty_view_sql("v_market_state_inputs", cols))
        return
    rise_fall = _latest_or_empty(
        con,
        "market_rise_fall",
        ["date"],
        [
            ("date", "DATE"),
            ("broken_limit_up_count", "INTEGER"),
            ("blown_limit_up_count", "INTEGER"),
            ("blown_limit_up_rate", "DOUBLE"),
        ],
    )
    emotion = _latest_or_empty(
        con,
        "market_emotion_money",
        ["date"],
        [
            ("date", "DATE"),
            ("cgl", "DOUBLE"),
            ("yll", "DOUBLE"),
            ("success_rate", "DOUBLE"),
        ],
    )
    new_high = _latest_or_empty(
        con,
        "daily_new_high",
        ["date"],
        [("date", "DATE"), ("count", "INTEGER")],
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_market_state_inputs AS
        SELECT
            m.trade_date,
            m.limit_up_count,
            m.limit_down_count,
            m.rise_count,
            m.fall_count,
            m.consecutive_count,
            r.broken_limit_up_count,
            r.blown_limit_up_count,
            r.blown_limit_up_rate,
            e.cgl,
            e.yll,
            e.success_rate,
            n.count AS new_high_count,
            round(
                coalesce(e.success_rate, 0) * 0.4
                + CASE
                    WHEN coalesce(m.rise_count, 0) + coalesce(m.fall_count, 0) > 0
                    THEN m.rise_count * 100.0 / (m.rise_count + m.fall_count) * 0.4
                    ELSE 0
                  END
                + least(coalesce(m.limit_up_count, 0), 100) * 0.2,
                2
            ) AS earning_effect_score,
            round(
                least(
                    100.0,
                    greatest(
                        0.0,
                        coalesce(r.blown_limit_up_rate, 0) * 0.35
                        + least(coalesce(m.limit_down_count, 0), 80) * 0.85
                        + least(coalesce(r.broken_limit_up_count, 0), 80) * 0.55
                        + least(coalesce(r.blown_limit_up_count, 0), 100) * 0.35
                    )
                ),
                2
            ) AS acute_drop_risk_score,
            m.market_daily_source || '+market_rise_fall+market_emotion_money' AS source_table,
            m.is_fallback,
            m.fetched_at
        FROM v_market_daily m
        LEFT JOIN ({rise_fall}) r ON m.trade_date = CAST(r.date AS VARCHAR)
        LEFT JOIN ({emotion}) e ON m.trade_date = CAST(e.date AS VARCHAR)
        LEFT JOIN ({new_high}) n ON m.trade_date = CAST(n.date AS VARCHAR)
        """
    )


def _create_index_state(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("index_code", "VARCHAR"),
        ("open", "DOUBLE"),
        ("high", "DOUBLE"),
        ("low", "DOUBLE"),
        ("close", "DOUBLE"),
        ("change_pct", "DOUBLE"),
        ("turnover", "BIGINT"),
        ("source_table", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
        ("fetched_at", "TIMESTAMP"),
    ]
    if _relation_has_rows(con, "index_kline"):
        latest = _latest_cte("index_kline", ["date", "index_code", "ktype"], _timestamp_column(con, "index_kline"))
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_index_state AS
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                index_code,
                open,
                high,
                low,
                close,
                change_pct,
                turnover,
                'index_kline' AS source_table,
                false AS is_fallback,
                fetched_at
            FROM ({latest})
            """
        )
        return
    if _relation_has_rows(con, "index_intraday"):
        latest = _latest_cte(
            "index_intraday",
            ["date", "index_code", "time"],
            _timestamp_column(con, "index_intraday"),
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_index_state AS
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                index_code,
                CAST(NULL AS DOUBLE) AS open,
                CAST(NULL AS DOUBLE) AS high,
                CAST(NULL AS DOUBLE) AS low,
                price AS close,
                CAST(NULL AS DOUBLE) AS change_pct,
                turnover,
                'index_intraday' AS source_table,
                false AS is_fallback,
                fetched_at
            FROM ({latest})
            """
        )
        return
    if _relation_has_rows(con, "l2_realtime_index_list"):
        cols_l2_index = table_columns(con, "l2_realtime_index_list")
        turnover_expr = "turnover" if "turnover" in cols_l2_index else "CAST(NULL AS BIGINT)"
        fetched_expr = "fetched_at" if "fetched_at" in cols_l2_index else "current_timestamp"
        latest = _latest_cte(
            "l2_realtime_index_list",
            ["date", "index_code"],
            _timestamp_column(con, "l2_realtime_index_list"),
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_index_state AS
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                index_code,
                CAST(NULL AS DOUBLE) AS open,
                CAST(NULL AS DOUBLE) AS high,
                CAST(NULL AS DOUBLE) AS low,
                price AS close,
                change_pct,
                {turnover_expr} AS turnover,
                'l2_realtime_index_list' AS source_table,
                false AS is_fallback,
                {fetched_expr} AS fetched_at
            FROM ({latest})
            """
        )
        return
    if _relation_has_rows(con, "v_market_daily"):
        con.execute(
            """
            CREATE OR REPLACE VIEW v_index_state AS
            SELECT
                trade_date,
                'MARKET_PROXY' AS index_code,
                CAST(NULL AS DOUBLE) AS open,
                CAST(NULL AS DOUBLE) AS high,
                CAST(NULL AS DOUBLE) AS low,
                CAST(NULL AS DOUBLE) AS close,
                CASE
                    WHEN coalesce(rise_count, 0) + coalesce(fall_count, 0) > 0
                    THEN (rise_count - fall_count) * 100.0 / (rise_count + fall_count)
                    ELSE NULL
                END AS change_pct,
                CAST(NULL AS BIGINT) AS turnover,
                'market_state_fallback' AS source_table,
                true AS is_fallback,
                fetched_at
            FROM v_market_daily
            """
        )
        return
    con.execute(_empty_view_sql("v_index_state", cols))


def _create_intraday_capital_flow_evidence(con: duckdb.DuckDBPyConnection) -> None:
    _unused_cols = [
        ("trade_date", "VARCHAR"),
        ("stock_code", "VARCHAR"),
        ("main_monitor_net_inflow", "BIGINT"),
        ("main_monitor_best_rank", "INTEGER"),
        ("zjmm_main_net_inflow", "BIGINT"),
        ("zjmm_super_net_inflow", "BIGINT"),
        ("zjmm_big_net_inflow", "BIGINT"),
        ("zjmm_rows", "BIGINT"),
        ("dadan_big_net_amount", "BIGINT"),
        ("main_activity_score", "DOUBLE"),
        ("tick_rows", "BIGINT"),
        ("tick_volume", "BIGINT"),
        ("order_flow_rows", "BIGINT"),
        ("order_flow_volume", "BIGINT"),
        ("pankou_net_volume", "BIGINT"),
        ("capital_flow_score", "DOUBLE"),
        ("source_tables", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
    ]
    empty = _empty_relation_sql

    if _table_has_columns(con, "advanced_main_monitor", ["date", "stock_code"]):
        monitor_cols = set(table_columns(con, "advanced_main_monitor"))
        amount_expr = "sum(coalesce(main_net_inflow, 0))" if "main_net_inflow" in monitor_cols else "CAST(NULL AS BIGINT)"
        rank_expr = "min(ranking)" if "ranking" in monitor_cols else "CAST(NULL AS INTEGER)"
        monitor_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                {amount_expr} AS main_monitor_net_inflow,
                {rank_expr} AS main_monitor_best_rank
            FROM advanced_main_monitor
            GROUP BY date, stock_code
        """
    else:
        monitor_sql = empty(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                ("main_monitor_net_inflow", "BIGINT"),
                ("main_monitor_best_rank", "INTEGER"),
            ]
        )

    if _table_has_columns(con, "advanced_zjmm_min", ["date", "stock_code"]):
        zjmm_cols = set(table_columns(con, "advanced_zjmm_min"))
        main_expr = "sum(coalesce(main_net_inflow, 0))" if "main_net_inflow" in zjmm_cols else "CAST(NULL AS BIGINT)"
        super_expr = "sum(coalesce(super_net_inflow, 0))" if "super_net_inflow" in zjmm_cols else "CAST(NULL AS BIGINT)"
        big_expr = "sum(coalesce(big_net_inflow, 0))" if "big_net_inflow" in zjmm_cols else "CAST(NULL AS BIGINT)"
        zjmm_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                {main_expr} AS zjmm_main_net_inflow,
                {super_expr} AS zjmm_super_net_inflow,
                {big_expr} AS zjmm_big_net_inflow,
                count(*) AS zjmm_rows
            FROM advanced_zjmm_min
            GROUP BY date, stock_code
        """
    else:
        zjmm_sql = empty(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                ("zjmm_main_net_inflow", "BIGINT"),
                ("zjmm_super_net_inflow", "BIGINT"),
                ("zjmm_big_net_inflow", "BIGINT"),
                ("zjmm_rows", "BIGINT"),
            ]
        )

    dadan_parts = []
    for table_name in ("advanced_dadan_kline", "advanced_dadan_kline_today", "advanced_kline_today_dadan_new"):
        if _table_has_columns(con, table_name, ["date", "stock_code"]):
            table_cols = set(table_columns(con, table_name))
            amount_expr = "big_net_amount" if "big_net_amount" in table_cols else "0"
            # The THS daily large-order endpoint has used both ``D`` and ``1``
            # for its daily record across API versions.  Minute values (5/15/
            # 30/60) must not be mixed into the daily evidence or the UNION
            # would multiply the daily net amount several times over.
            ktype_filter = (
                "WHERE upper(coalesce(ktype, 'D')) IN ('D', '1')"
                if "ktype" in table_cols else ""
            )
            dadan_parts.append(
                f"""
                SELECT
                    CAST(date AS VARCHAR) AS trade_date,
                    stock_code,
                    coalesce({amount_expr}, 0) AS dadan_big_net_amount
                FROM {table_name}
                {ktype_filter}
                """
            )
    if dadan_parts:
        dadan_sql = f"""
            SELECT trade_date, stock_code, sum(dadan_big_net_amount) AS dadan_big_net_amount
            FROM ({" UNION ALL ".join(dadan_parts)})
            GROUP BY trade_date, stock_code
        """
    else:
        dadan_sql = empty([("trade_date", "VARCHAR"), ("stock_code", "VARCHAR"), ("dadan_big_net_amount", "BIGINT")])

    activity_parts = []
    for table_name in ("advanced_main_activity_kline", "advanced_kline_today_main_activity"):
        if _table_has_columns(con, table_name, ["date", "stock_code"]):
            table_cols = set(table_columns(con, table_name))
            score_expr = "main_activity_score" if "main_activity_score" in table_cols else "0"
            activity_parts.append(
                f"""
                SELECT
                    CAST(date AS VARCHAR) AS trade_date,
                    stock_code,
                    coalesce({score_expr}, 0) AS main_activity_score
                FROM {table_name}
                """
            )
    if activity_parts:
        activity_sql = f"""
            SELECT trade_date, stock_code, max(main_activity_score) AS main_activity_score
            FROM ({" UNION ALL ".join(activity_parts)})
            GROUP BY trade_date, stock_code
        """
    else:
        activity_sql = empty([("trade_date", "VARCHAR"), ("stock_code", "VARCHAR"), ("main_activity_score", "DOUBLE")])

    if _table_has_columns(con, "l2_tick_history", ["date", "stock_code"]):
        tick_cols = set(table_columns(con, "l2_tick_history"))
        volume_expr = "sum(coalesce(volume, 0))" if "volume" in tick_cols else "CAST(NULL AS BIGINT)"
        tick_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                count(*) AS tick_rows,
                {volume_expr} AS tick_volume
            FROM l2_tick_history
            GROUP BY date, stock_code
        """
    else:
        tick_sql = empty(
            [("trade_date", "VARCHAR"), ("stock_code", "VARCHAR"), ("tick_rows", "BIGINT"), ("tick_volume", "BIGINT")]
        )

    if _table_has_columns(con, "l2_tick_orders_all", ["date", "stock_code"]):
        order_cols = set(table_columns(con, "l2_tick_orders_all"))
        volume_expr = "sum(coalesce(volume, 0))" if "volume" in order_cols else "CAST(NULL AS BIGINT)"
        order_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                count(*) AS order_flow_rows,
                {volume_expr} AS order_flow_volume
            FROM l2_tick_orders_all
            GROUP BY date, stock_code
        """
    else:
        order_sql = empty(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                ("order_flow_rows", "BIGINT"),
                ("order_flow_volume", "BIGINT"),
            ]
        )

    if _table_has_columns(con, "advanced_pankou", ["date", "stock_code"]):
        pankou_cols = set(table_columns(con, "advanced_pankou"))
        buy_expr = "buy1_volume" if "buy1_volume" in pankou_cols else "0"
        sell_expr = "sell1_volume" if "sell1_volume" in pankou_cols else "0"
        pankou_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                sum(coalesce({buy_expr}, 0) - coalesce({sell_expr}, 0)) AS pankou_net_volume
            FROM advanced_pankou
            GROUP BY date, stock_code
        """
    else:
        pankou_sql = empty([("trade_date", "VARCHAR"), ("stock_code", "VARCHAR"), ("pankou_net_volume", "BIGINT")])

    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_intraday_capital_flow_evidence AS
        WITH
        monitor AS ({monitor_sql}),
        zjmm AS ({zjmm_sql}),
        dadan AS ({dadan_sql}),
        activity AS ({activity_sql}),
        tick_history AS ({tick_sql}),
        order_flow AS ({order_sql}),
        pankou AS ({pankou_sql}),
        keys AS (
            SELECT trade_date, stock_code FROM monitor
            UNION SELECT trade_date, stock_code FROM zjmm
            UNION SELECT trade_date, stock_code FROM dadan
            UNION SELECT trade_date, stock_code FROM activity
            UNION SELECT trade_date, stock_code FROM tick_history
            UNION SELECT trade_date, stock_code FROM order_flow
            UNION SELECT trade_date, stock_code FROM pankou
        ),
        joined AS (
            SELECT
                k.trade_date,
                k.stock_code,
                m.main_monitor_net_inflow,
                m.main_monitor_best_rank,
                z.zjmm_main_net_inflow,
                z.zjmm_super_net_inflow,
                z.zjmm_big_net_inflow,
                z.zjmm_rows,
                d.dadan_big_net_amount,
                a.main_activity_score,
                th.tick_rows,
                th.tick_volume,
                ofl.order_flow_rows,
                ofl.order_flow_volume,
                p.pankou_net_volume
            FROM keys k
            LEFT JOIN monitor m ON k.trade_date = m.trade_date AND k.stock_code = m.stock_code
            LEFT JOIN zjmm z ON k.trade_date = z.trade_date AND k.stock_code = z.stock_code
            LEFT JOIN dadan d ON k.trade_date = d.trade_date AND k.stock_code = d.stock_code
            LEFT JOIN activity a ON k.trade_date = a.trade_date AND k.stock_code = a.stock_code
            LEFT JOIN tick_history th ON k.trade_date = th.trade_date AND k.stock_code = th.stock_code
            LEFT JOIN order_flow ofl ON k.trade_date = ofl.trade_date AND k.stock_code = ofl.stock_code
            LEFT JOIN pankou p ON k.trade_date = p.trade_date AND k.stock_code = p.stock_code
        )
        SELECT
            trade_date,
            stock_code,
            main_monitor_net_inflow,
            main_monitor_best_rank,
            zjmm_main_net_inflow,
            zjmm_super_net_inflow,
            zjmm_big_net_inflow,
            zjmm_rows,
            dadan_big_net_amount,
            main_activity_score,
            tick_rows,
            tick_volume,
            order_flow_rows,
            order_flow_volume,
            pankou_net_volume,
            round(
                greatest(-20.0, least(20.0, coalesce(main_monitor_net_inflow, 0) / 10000000.0))
                + greatest(-20.0, least(20.0, coalesce(zjmm_main_net_inflow, 0) / 10000000.0))
                + greatest(-20.0, least(20.0, coalesce(dadan_big_net_amount, 0) / 10000000.0))
                + greatest(-5.0, least(15.0, coalesce(main_activity_score, 0) / 10.0))
                + greatest(-5.0, least(10.0, coalesce(tick_volume, 0) / 100000.0))
                + greatest(-5.0, least(10.0, coalesce(order_flow_volume, 0) / 100000.0))
                + greatest(-5.0, least(5.0, coalesce(pankou_net_volume, 0) / 100000.0)),
                4
            ) AS capital_flow_score,
            'advanced_main_monitor+advanced_zjmm_min+advanced_dadan_kline+advanced_main_activity_kline+l2_tick_history+l2_tick_orders_all+advanced_pankou' AS source_tables,
            false AS is_fallback
        FROM joined
        WHERE trade_date IS NOT NULL AND stock_code IS NOT NULL
        """
    )


def _create_intraday_strength_evidence(con: duckdb.DuckDBPyConnection) -> None:
    _unused_cols = [
        ("trade_date", "VARCHAR"),
        ("stock_code", "VARCHAR"),
        ("intraday_high", "DOUBLE"),
        ("active_fund_net", "DOUBLE"),
        ("intraday_turnover", "BIGINT"),
        ("big_net_amount", "DOUBLE"),
        ("tick_rows", "BIGINT"),
        ("tick_volume", "BIGINT"),
        ("tick_order_rows", "BIGINT"),
        ("tick_order_volume", "BIGINT"),
        ("tick_all_rows", "BIGINT"),
        ("tick_all_volume", "BIGINT"),
        ("pankou_net_volume", "BIGINT"),
        ("capital_flow_score", "DOUBLE"),
        ("capital_flow_source_tables", "VARCHAR"),
        ("strength_score", "DOUBLE"),
        ("source_tables", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
    ]
    empty = _empty_relation_sql
    if _table_has_columns(con, "l2_stock_intraday", ["date", "stock_code"]):
        intraday_cols = set(table_columns(con, "l2_stock_intraday"))
        high_expr = "max(price)" if "price" in intraday_cols else "CAST(NULL AS DOUBLE)"
        fund_expr = "max(main_fund_net)" if "main_fund_net" in intraday_cols else "CAST(NULL AS DOUBLE)"
        turnover_expr = "sum(turnover)" if "turnover" in intraday_cols else "CAST(NULL AS BIGINT)"
        intraday_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                {high_expr} AS intraday_high,
                {fund_expr} AS active_fund_net,
                {turnover_expr} AS intraday_turnover
            FROM l2_stock_intraday
            GROUP BY date, stock_code
        """
    else:
        intraday_sql = empty(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                ("intraday_high", "DOUBLE"),
                ("active_fund_net", "DOUBLE"),
                ("intraday_turnover", "BIGINT"),
            ]
        )

    if _table_has_columns(con, "l2_stock_bigorder", ["date", "stock_code"]):
        amount_expr = "sum(big_net_amount)" if "big_net_amount" in table_columns(con, "l2_stock_bigorder") else "CAST(NULL AS DOUBLE)"
        bigorder_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                {amount_expr} AS big_net_amount
            FROM l2_stock_bigorder
            GROUP BY date, stock_code
        """
    else:
        bigorder_sql = empty([("trade_date", "VARCHAR"), ("stock_code", "VARCHAR"), ("big_net_amount", "DOUBLE")])

    def tick_sql(table_name: str, rows_alias: str, volume_alias: str) -> str:
        if _table_has_columns(con, table_name, ["date", "stock_code"]):
            volume_expr = "sum(volume)" if "volume" in table_columns(con, table_name) else "CAST(NULL AS BIGINT)"
            return f"""
                SELECT
                    CAST(date AS VARCHAR) AS trade_date,
                    stock_code,
                    count(*) AS {rows_alias},
                    {volume_expr} AS {volume_alias}
                FROM {table_name}
                GROUP BY date, stock_code
            """
        return empty(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                (rows_alias, "BIGINT"),
                (volume_alias, "BIGINT"),
            ]
        )

    tick_history_sql = tick_sql("l2_tick_history", "tick_rows", "tick_volume")
    tick_orders_sql = tick_sql("l2_tick_orders", "tick_order_rows", "tick_order_volume")
    tick_orders_all_sql = tick_sql("l2_tick_orders_all", "tick_all_rows", "tick_all_volume")

    if _table_has_columns(con, "advanced_pankou", ["date", "stock_code"]):
        pankou_cols = set(table_columns(con, "advanced_pankou"))
        buy_expr = "buy1_volume" if "buy1_volume" in pankou_cols else "0"
        sell_expr = "sell1_volume" if "sell1_volume" in pankou_cols else "0"
        pankou_sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                stock_code,
                sum(coalesce({buy_expr}, 0) - coalesce({sell_expr}, 0)) AS pankou_net_volume
            FROM advanced_pankou
            GROUP BY date, stock_code
        """
    else:
        pankou_sql = empty([("trade_date", "VARCHAR"), ("stock_code", "VARCHAR"), ("pankou_net_volume", "BIGINT")])

    if table_exists(con, "v_intraday_capital_flow_evidence"):
        capital_sql = """
            SELECT
                trade_date,
                stock_code,
                capital_flow_score,
                source_tables AS capital_flow_source_tables
            FROM v_intraday_capital_flow_evidence
        """
    else:
        capital_sql = empty(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                ("capital_flow_score", "DOUBLE"),
                ("capital_flow_source_tables", "VARCHAR"),
            ]
        )

    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_intraday_strength_evidence AS
        WITH
        intraday AS ({intraday_sql}),
        bigorder AS ({bigorder_sql}),
        tick_history AS ({tick_history_sql}),
        tick_orders AS ({tick_orders_sql}),
        tick_orders_all AS ({tick_orders_all_sql}),
        pankou AS ({pankou_sql}),
        capital AS ({capital_sql}),
        joined AS (
            SELECT
                coalesce(i.trade_date, b.trade_date, th.trade_date, tor.trade_date, toa.trade_date, p.trade_date, c.trade_date) AS trade_date,
                coalesce(i.stock_code, b.stock_code, th.stock_code, tor.stock_code, toa.stock_code, p.stock_code, c.stock_code) AS stock_code,
                i.intraday_high,
                i.active_fund_net,
                i.intraday_turnover,
                b.big_net_amount,
                th.tick_rows,
                th.tick_volume,
                tor.tick_order_rows,
                tor.tick_order_volume,
                toa.tick_all_rows,
                toa.tick_all_volume,
                p.pankou_net_volume,
                c.capital_flow_score,
                c.capital_flow_source_tables
            FROM intraday i
            FULL OUTER JOIN bigorder b
              ON i.trade_date = b.trade_date AND i.stock_code = b.stock_code
            FULL OUTER JOIN tick_history th
              ON coalesce(i.trade_date, b.trade_date) = th.trade_date
             AND coalesce(i.stock_code, b.stock_code) = th.stock_code
            FULL OUTER JOIN tick_orders tor
              ON coalesce(i.trade_date, b.trade_date, th.trade_date) = tor.trade_date
             AND coalesce(i.stock_code, b.stock_code, th.stock_code) = tor.stock_code
            FULL OUTER JOIN tick_orders_all toa
              ON coalesce(i.trade_date, b.trade_date, th.trade_date, tor.trade_date) = toa.trade_date
             AND coalesce(i.stock_code, b.stock_code, th.stock_code, tor.stock_code) = toa.stock_code
            FULL OUTER JOIN pankou p
              ON coalesce(i.trade_date, b.trade_date, th.trade_date, tor.trade_date, toa.trade_date) = p.trade_date
             AND coalesce(i.stock_code, b.stock_code, th.stock_code, tor.stock_code, toa.stock_code) = p.stock_code
            FULL OUTER JOIN capital c
              ON coalesce(i.trade_date, b.trade_date, th.trade_date, tor.trade_date, toa.trade_date, p.trade_date) = c.trade_date
             AND coalesce(i.stock_code, b.stock_code, th.stock_code, tor.stock_code, toa.stock_code, p.stock_code) = c.stock_code
        )
        SELECT
            trade_date,
            stock_code,
            intraday_high,
            active_fund_net,
            intraday_turnover,
            big_net_amount,
            tick_rows,
            tick_volume,
            tick_order_rows,
            tick_order_volume,
            tick_all_rows,
            tick_all_volume,
            pankou_net_volume,
            capital_flow_score,
            capital_flow_source_tables,
            round(
                coalesce(active_fund_net, 0) / 10000000.0
                + coalesce(big_net_amount, 0) / 10000000.0
                + coalesce(intraday_turnover, 0) / 100000000.0
                + coalesce(tick_volume, 0) / 100000.0
                + coalesce(tick_order_volume, 0) / 100000.0
                + coalesce(tick_all_volume, 0) / 100000.0
                + coalesce(pankou_net_volume, 0) / 100000.0
                + coalesce(capital_flow_score, 0),
                4
            ) AS strength_score,
            'l2_stock_intraday+l2_stock_bigorder+l2_tick_history+l2_tick_orders+l2_tick_orders_all+advanced_pankou+v_intraday_capital_flow_evidence' AS source_tables,
            false AS is_fallback
        FROM joined
        WHERE trade_date IS NOT NULL AND stock_code IS NOT NULL
        """
    )


def _create_theme_mainline_evidence(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("sector_code", "VARCHAR"),
        ("sector_name", "VARCHAR"),
        ("sector_type", "VARCHAR"),
        ("strength_value", "DOUBLE"),
        ("limit_up_count", "INTEGER"),
        ("seal_rate", "DOUBLE"),
        ("main_net_inflow", "BIGINT"),
        ("component_count", "BIGINT"),
        ("son_plate_count", "BIGINT"),
        ("sub_concept_count", "BIGINT"),
        ("boom_reason", "VARCHAR"),
        ("mainline_score", "DOUBLE"),
        ("source_tables", "VARCHAR"),
        ("is_fallback", "BOOLEAN"),
    ]
    if _relation_has_rows(con, "v_sector_capital"):
        base_sql = """
            SELECT
                trade_date,
                sector_code,
                sector_name,
                sector_type,
                strength_value,
                limit_up_count,
                seal_rate,
                main_net_inflow,
                is_fallback
            FROM v_sector_capital
        """
    elif _relation_has_rows(con, "v_sector_daily"):
        base_sql = """
            SELECT
                trade_date,
                sector_code,
                sector_name,
                CASE WHEN sector_code LIKE 'THS-%' THEN 'ths_concept' ELSE 'em_industry' END AS sector_type,
                strength_value,
                limit_up_count,
                seal_rate,
                CAST(NULL AS BIGINT) AS main_net_inflow,
                true AS is_fallback
            FROM v_sector_daily
        """
    else:
        con.execute(_empty_view_sql("v_theme_mainline_evidence", cols))
        return

    # Operational features must consume the quality-gated default relation.
    # The raw THS table intentionally retains stale/partial snapshots for
    # audit and recovery, so reading it directly would leak those rows into
    # normalized strategy evidence.
    component_source = (
        "v_default_concept_stock_history"
        if _table_has_columns(con, "v_default_concept_stock_history", ["trade_date", "concept_code", "stock_code"])
        else "ths_concept_stock_history"
        if _table_has_columns(con, "ths_concept_stock_history", ["trade_date", "concept_code", "stock_code"])
        else "sector_all_stocks"
    )
    if component_source in {"v_default_concept_stock_history", "ths_concept_stock_history"}:
        component_sql = """
            SELECT
                CAST(trade_date AS DATE) AS membership_date,
                CAST(concept_code AS VARCHAR) AS sector_code,
                count(DISTINCT regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '')) AS component_count
            FROM {source}
            GROUP BY trade_date, concept_code
        """.format(source=component_source)
    elif _table_has_columns(con, "sector_all_stocks", ["date", "sector_code", "stock_code"]):
        component_sql = """
            SELECT
                CAST(date AS DATE) AS membership_date,
                CAST(sector_code AS VARCHAR) AS sector_code,
                count(DISTINCT stock_code) AS component_count
            FROM sector_all_stocks
            GROUP BY date, sector_code
        """
    else:
        component_sql = _empty_relation_sql(
            [("membership_date", "DATE"), ("sector_code", "VARCHAR"), ("component_count", "BIGINT")]
        )

    if _table_has_columns(con, "sector_son_plates", ["parent_code", "son_code"]):
        son_sql = """
            SELECT parent_code AS sector_code, count(DISTINCT son_code) AS son_plate_count
            FROM sector_son_plates
            GROUP BY parent_code
        """
    else:
        son_sql = _empty_relation_sql([("sector_code", "VARCHAR"), ("son_plate_count", "BIGINT")])

    if _table_has_columns(con, "sector_sub_concepts", ["sector_code", "concept_code"]):
        sub_sql = """
            SELECT sector_code, count(DISTINCT concept_code) AS sub_concept_count
            FROM sector_sub_concepts
            GROUP BY sector_code
        """
    else:
        sub_sql = _empty_relation_sql([("sector_code", "VARCHAR"), ("sub_concept_count", "BIGINT")])

    if _table_has_columns(con, "sector_boom_reason", ["date", "sector_code", "reason"]):
        reason_sql = """
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                sector_code,
                max(reason) AS boom_reason
            FROM sector_boom_reason
            GROUP BY date, sector_code
        """
    else:
        reason_sql = _empty_relation_sql(
            [("trade_date", "VARCHAR"), ("sector_code", "VARCHAR"), ("boom_reason", "VARCHAR")]
        )

    # Minimal legacy databases used by maintenance tools may not have gone
    # through init_schema yet.  Keep their industry-only evidence renderable;
    # once the canonical THS relation exists, enforce the full concept gate.
    if table_exists(con, "v_default_concept_daily"):
        taxonomy_gate = """
            WHERE coalesce(b.sector_type, CASE WHEN b.sector_code LIKE 'THS-%' THEN 'ths_concept' ELSE 'em_industry' END)
                  IN ('ths_concept', 'ths_concept_derived')
              AND EXISTS (
                  SELECT 1
                  FROM v_default_concept_daily d
                  WHERE d.trade_date = CAST(b.trade_date AS DATE)
                  GROUP BY d.trade_date
                  HAVING count(DISTINCT d.concept_code) > 0
              )
        """
    else:
        taxonomy_gate = "WHERE TRUE"

    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_theme_mainline_evidence AS
        WITH
        base AS ({base_sql}),
        components AS ({component_sql}),
        son AS ({son_sql}),
        sub_concepts AS ({sub_sql}),
        reasons AS ({reason_sql})
        SELECT
            b.trade_date,
            b.sector_code,
            b.sector_name,
            b.sector_type,
            b.strength_value,
            b.limit_up_count,
            b.seal_rate,
            b.main_net_inflow,
            coalesce(c.component_count, 0) AS component_count,
            coalesce(s.son_plate_count, 0) AS son_plate_count,
            coalesce(sc.sub_concept_count, 0) AS sub_concept_count,
            r.boom_reason,
            round(
                coalesce(b.strength_value, 0) * 0.55
                + coalesce(b.limit_up_count, 0) * 2.0
                + coalesce(b.seal_rate, 0) * 0.10
                + coalesce(b.main_net_inflow, 0) / 100000000.0
                + ln(1 + coalesce(c.component_count, 0)) * 1.50
                + coalesce(s.son_plate_count, 0) * 0.50
                + coalesce(sc.sub_concept_count, 0) * 0.50,
                4
            ) AS mainline_score,
            'v_sector_capital+{component_source}+sector_son_plates+sector_sub_concepts+sector_boom_reason' AS source_tables,
            b.is_fallback
        FROM base b
        LEFT JOIN components c
          ON c.sector_code = b.sector_code
         AND c.membership_date = (
             SELECT max(c2.membership_date)
             FROM components c2
             WHERE c2.sector_code = b.sector_code
               AND c2.membership_date <= CAST(b.trade_date AS DATE)
         )
        LEFT JOIN son s
          ON b.sector_code = s.sector_code
        LEFT JOIN sub_concepts sc
          ON b.sector_code = sc.sector_code
        LEFT JOIN reasons r
          ON b.trade_date = r.trade_date AND b.sector_code = r.sector_code
            {taxonomy_gate}
            """
    )


def _create_research_event_evidence(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("event_type", "VARCHAR"),
        ("symbol", "VARCHAR"),
        ("sector_code", "VARCHAR"),
        ("title", "VARCHAR"),
        ("source", "VARCHAR"),
        ("url", "VARCHAR"),
        ("risk_tags", "VARCHAR"),
        ("catalyst_tags", "VARCHAR"),
        ("source_table", "VARCHAR"),
        ("is_signal_input", "BOOLEAN"),
    ]
    parts: list[str] = []
    if _table_has_columns(con, "advanced_news_flash", ["date", "news_title"]):
        source_expr = "news_source" if "news_source" in table_columns(con, "advanced_news_flash") else "CAST(NULL AS VARCHAR)"
        url_expr = "news_url" if "news_url" in table_columns(con, "advanced_news_flash") else "CAST(NULL AS VARCHAR)"
        parts.append(
            f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'news_flash' AS event_type,
                CAST(NULL AS VARCHAR) AS symbol,
                CAST(NULL AS VARCHAR) AS sector_code,
                news_title AS title,
                {source_expr} AS source,
                {url_expr} AS url,
                CAST(NULL AS VARCHAR) AS risk_tags,
                'intraday_catalyst' AS catalyst_tags,
                'advanced_news_flash' AS source_table,
                false AS is_signal_input
            FROM advanced_news_flash
            WHERE news_title IS NOT NULL
            """
        )
    if _table_has_columns(con, "advanced_news_flash_top", ["date", "news_title"]):
        source_expr = "news_source" if "news_source" in table_columns(con, "advanced_news_flash_top") else "CAST(NULL AS VARCHAR)"
        parts.append(
            f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'news_flash_top' AS event_type,
                CAST(NULL AS VARCHAR) AS symbol,
                CAST(NULL AS VARCHAR) AS sector_code,
                news_title AS title,
                {source_expr} AS source,
                CAST(NULL AS VARCHAR) AS url,
                CAST(NULL AS VARCHAR) AS risk_tags,
                'top_catalyst' AS catalyst_tags,
                'advanced_news_flash_top' AS source_table,
                false AS is_signal_input
            FROM advanced_news_flash_top
            WHERE news_title IS NOT NULL
            """
        )
    if _table_has_columns(con, "news_plate", ["date", "sector_code", "news_title"]):
        source_expr = "news_source" if "news_source" in table_columns(con, "news_plate") else "CAST(NULL AS VARCHAR)"
        url_expr = "news_url" if "news_url" in table_columns(con, "news_plate") else "CAST(NULL AS VARCHAR)"
        parts.append(
            f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'plate_news' AS event_type,
                CAST(NULL AS VARCHAR) AS symbol,
                sector_code,
                news_title AS title,
                {source_expr} AS source,
                {url_expr} AS url,
                CAST(NULL AS VARCHAR) AS risk_tags,
                sector_code AS catalyst_tags,
                'news_plate' AS source_table,
                false AS is_signal_input
            FROM news_plate
            WHERE news_title IS NOT NULL
            """
        )
    if _table_has_columns(con, "news_theme", ["date", "news_title"]):
        theme_expr = "theme_name" if "theme_name" in table_columns(con, "news_theme") else "CAST(NULL AS VARCHAR)"
        url_expr = "news_url" if "news_url" in table_columns(con, "news_theme") else "CAST(NULL AS VARCHAR)"
        parts.append(
            f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'theme_news' AS event_type,
                CAST(NULL AS VARCHAR) AS symbol,
                CAST(NULL AS VARCHAR) AS sector_code,
                news_title AS title,
                {theme_expr} AS source,
                {url_expr} AS url,
                CAST(NULL AS VARCHAR) AS risk_tags,
                {theme_expr} AS catalyst_tags,
                'news_theme' AS source_table,
                false AS is_signal_input
            FROM news_theme
            WHERE news_title IS NOT NULL
            """
        )
    if _table_has_columns(con, "advanced_corporate_news", ["date", "stock_code", "news_title"]):
        type_expr = "news_type" if "news_type" in table_columns(con, "advanced_corporate_news") else "CAST(NULL AS VARCHAR)"
        parts.append(
            f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'corporate_news' AS event_type,
                stock_code AS symbol,
                CAST(NULL AS VARCHAR) AS sector_code,
                news_title AS title,
                {type_expr} AS source,
                CAST(NULL AS VARCHAR) AS url,
                {type_expr} AS risk_tags,
                CAST(NULL AS VARCHAR) AS catalyst_tags,
                'advanced_corporate_news' AS source_table,
                false AS is_signal_input
            FROM advanced_corporate_news
            WHERE news_title IS NOT NULL
            """
        )
    if _table_has_columns(con, "advanced_interviews", ["date", "stock_code"]):
        stock_name_expr = "stock_name" if "stock_name" in table_columns(con, "advanced_interviews") else "stock_code"
        count_expr = "institution_count" if "institution_count" in table_columns(con, "advanced_interviews") else "CAST(NULL AS INTEGER)"
        parts.append(
            f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'institution_interview' AS event_type,
                stock_code AS symbol,
                CAST(NULL AS VARCHAR) AS sector_code,
                concat({stock_name_expr}, ' institution interview count ', coalesce(CAST({count_expr} AS VARCHAR), '')) AS title,
                'advanced_interviews' AS source,
                CAST(NULL AS VARCHAR) AS url,
                CAST(NULL AS VARCHAR) AS risk_tags,
                'institution_attention' AS catalyst_tags,
                'advanced_interviews' AS source_table,
                false AS is_signal_input
            FROM advanced_interviews
            """
        )
    if _table_has_columns(con, "tuyere_by_stock", ["date", "stock_code", "report_title"]):
        type_expr = "report_type" if "report_type" in table_columns(con, "tuyere_by_stock") else "CAST(NULL AS VARCHAR)"
        url_expr = "report_url" if "report_url" in table_columns(con, "tuyere_by_stock") else "CAST(NULL AS VARCHAR)"
        parts.append(
            f"""
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'tuyere_report' AS event_type,
                stock_code AS symbol,
                CAST(NULL AS VARCHAR) AS sector_code,
                report_title AS title,
                {type_expr} AS source,
                {url_expr} AS url,
                CAST(NULL AS VARCHAR) AS risk_tags,
                'research_report' AS catalyst_tags,
                'tuyere_by_stock' AS source_table,
                false AS is_signal_input
            FROM tuyere_by_stock
            WHERE report_title IS NOT NULL
            """
        )
    if _table_has_columns(con, "news_radar_item", ["trade_date", "title"]):
        parts.append(
            """
            SELECT
                trade_date,
                'news_radar' AS event_type,
                related_symbol AS symbol,
                related_sector AS sector_code,
                title,
                source,
                url,
                risk_tags,
                catalyst_tags,
                'news_radar_item' AS source_table,
                false AS is_signal_input
            FROM news_radar_item
            WHERE title IS NOT NULL
            """
        )
    if not parts:
        con.execute(_empty_view_sql("v_research_event_evidence", cols))
        return
    con.execute("CREATE OR REPLACE VIEW v_research_event_evidence AS " + " UNION ALL ".join(parts))


def _create_lhb_review_evidence(con: duckdb.DuckDBPyConnection) -> None:
    cols = [
        ("trade_date", "VARCHAR"),
        ("stock_code", "VARCHAR"),
        ("stock_name", "VARCHAR"),
        ("reason", "VARCHAR"),
        ("buy_amount", "BIGINT"),
        ("sell_amount", "BIGINT"),
        ("net_amount", "BIGINT"),
        ("broker_count", "BIGINT"),
        ("detail_net_amount", "BIGINT"),
        ("youzi_buy_amount", "BIGINT"),
        ("youzi_sell_amount", "BIGINT"),
        ("agency_buy_amount", "BIGINT"),
        ("business_buy_amount", "BIGINT"),
        ("on_lhb_probability", "DOUBLE"),
        ("source_table", "VARCHAR"),
        ("is_signal_input", "BOOLEAN"),
    ]
    if not _relation_has_rows(con, "v_lhb_daily"):
        con.execute(_empty_view_sql("v_lhb_review_evidence", cols))
        return

    detail_sql = (
        """
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            stock_code,
            count(DISTINCT broker_name) AS broker_count,
            sum(net_amount) AS detail_net_amount
        FROM lhb_detail
        GROUP BY date, stock_code
        """
        if _table_has_columns(con, "lhb_detail", ["date", "stock_code", "broker_name", "net_amount"])
        else _empty_relation_sql(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                ("broker_count", "BIGINT"),
                ("detail_net_amount", "BIGINT"),
            ]
        )
    )
    youzi_sql = (
        """
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            stock_code,
            sum(buy_amount) AS youzi_buy_amount,
            sum(sell_amount) AS youzi_sell_amount
        FROM lhb_youzi_dongxiang
        GROUP BY date, stock_code
        """
        if _table_has_columns(con, "lhb_youzi_dongxiang", ["date", "stock_code", "buy_amount", "sell_amount"])
        else _empty_relation_sql(
            [
                ("trade_date", "VARCHAR"),
                ("stock_code", "VARCHAR"),
                ("youzi_buy_amount", "BIGINT"),
                ("youzi_sell_amount", "BIGINT"),
            ]
        )
    )
    on_lhb_sql = (
        """
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            stock_code,
            max(probability) AS on_lhb_probability
        FROM advanced_on_the_lhb
        GROUP BY date, stock_code
        """
        if _table_has_columns(con, "advanced_on_the_lhb", ["date", "stock_code", "probability"])
        else _empty_relation_sql(
            [("trade_date", "VARCHAR"), ("stock_code", "VARCHAR"), ("on_lhb_probability", "DOUBLE")]
        )
    )
    agency_sql = (
        """
        SELECT CAST(date AS VARCHAR) AS trade_date, sum(buy_amount) AS agency_buy_amount
        FROM advanced_agency_list
        GROUP BY date
        """
        if _table_has_columns(con, "advanced_agency_list", ["date", "buy_amount"])
        else _empty_relation_sql([("trade_date", "VARCHAR"), ("agency_buy_amount", "BIGINT")])
    )
    business_sql = (
        """
        SELECT CAST(date AS VARCHAR) AS trade_date, sum(buy_amount) AS business_buy_amount
        FROM advanced_business_list
        GROUP BY date
        """
        if _table_has_columns(con, "advanced_business_list", ["date", "buy_amount"])
        else _empty_relation_sql([("trade_date", "VARCHAR"), ("business_buy_amount", "BIGINT")])
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW v_lhb_review_evidence AS
        WITH
        detail AS ({detail_sql}),
        youzi AS ({youzi_sql}),
        on_lhb AS ({on_lhb_sql}),
        agency AS ({agency_sql}),
        business AS ({business_sql})
        SELECT
            l.trade_date,
            l.stock_code,
            l.stock_name,
            l.reason,
            l.buy_amount,
            l.sell_amount,
            l.net_amount,
            d.broker_count,
            d.detail_net_amount,
            y.youzi_buy_amount,
            y.youzi_sell_amount,
            a.agency_buy_amount,
            bs.business_buy_amount,
            o.on_lhb_probability,
            'v_lhb_daily+lhb_detail+lhb_youzi_dongxiang+advanced_on_the_lhb+advanced_agency_list+advanced_business_list' AS source_table,
            false AS is_signal_input
        FROM v_lhb_daily l
        LEFT JOIN detail d
          ON l.trade_date = d.trade_date AND l.stock_code = d.stock_code
        LEFT JOIN youzi y
          ON l.trade_date = y.trade_date AND l.stock_code = y.stock_code
        LEFT JOIN on_lhb o
          ON l.trade_date = o.trade_date AND l.stock_code = o.stock_code
        LEFT JOIN agency a
          ON l.trade_date = a.trade_date
        LEFT JOIN business bs
          ON l.trade_date = bs.trade_date
        """
    )


def _create_data_coverage(con: duckdb.DuckDBPyConnection) -> None:
    table_names = [
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name NOT LIKE 'v_%' ORDER BY table_name"
        ).fetchall()
    ]
    if not table_names:
        con.execute(
            "CREATE OR REPLACE VIEW v_data_coverage AS "
            "SELECT CAST(NULL AS VARCHAR) AS table_name, CAST(NULL AS BIGINT) AS row_count WHERE false"
        )
        return
    parts = [f"SELECT '{name}' AS table_name, count(*) AS row_count FROM \"{name}\"" for name in table_names]
    con.execute("CREATE OR REPLACE VIEW v_data_coverage AS " + " UNION ALL ".join(parts))


def build_normalized_views(db_path: str | Path) -> list[str]:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        _refresh_default_concept_views(con)
        _create_market_daily(con)
        _create_sector_daily(con)
        _create_limit_pool(con)
        _create_limit_pool_rich(con)
        _create_lhb_daily(con)
        _create_stock_pool(con)
        _create_auction_status(con)
        _create_sector_capital(con)
        _create_kline_daily(con)
        _create_market_state_inputs(con)
        _create_index_state(con)
        _create_intraday_capital_flow_evidence(con)
        _create_intraday_strength_evidence(con)
        _create_theme_mainline_evidence(con)
        _create_research_event_evidence(con)
        _create_lhb_review_evidence(con)
        _create_data_coverage(con)
        return [
            "v_default_concept_daily",
            "v_default_concept_stock_history",
            "v_market_daily",
            "v_sector_daily",
            "v_limit_pool",
            "v_lhb_daily",
            "v_stock_pool",
            "v_auction_status",
            "v_sector_capital",
            "v_kline_daily",
            "v_market_state_inputs",
            "v_index_state",
            "v_intraday_capital_flow_evidence",
            "v_intraday_strength_evidence",
            "v_theme_mainline_evidence",
            "v_research_event_evidence",
            "v_lhb_review_evidence",
            "v_data_coverage",
        ]
    finally:
        con.close()
