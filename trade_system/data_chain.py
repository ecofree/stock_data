"""Assess availability of professional trading data chains."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.quality import table_columns

try:
    from base import connect_duckdb
except Exception:  # pragma: no cover - test isolation without project root
    connect_duckdb = None  # type: ignore


DATA_CHAINS = [
    {
        "chain": "集合竞价",
        "required": ["auction_bidding_anomaly", "auction_tick"],
        "fallback": ["advanced_morning_bidding_summary", "advanced_morning_bidding_list"],
        "why": "开盘前确认抢筹、出货、板块竞价方向。",
    },
    {
        "chain": "板块资金",
        "required": ["v_sector_capital", "sector_capital"],
        "fallback": ["sector_strength", "sector_ranking", "sector_boom_reason"],
        "why": "验证板块强度是否有资金推动。",
    },
    {
        # Prefer the normalized / TuShare path; physical KPL ``kline`` often
        # stalls while v_kline_daily is current.
        "chain": "基础K线",
        "required": ["v_kline_daily", "tushare_daily"],
        "fallback": ["kline", "advanced_gujia_kline", "advanced_dadan_kline"],
        "why": "用于回测、趋势过滤、候选股风险确认。",
    },
    {
        "chain": "指数",
        "required": ["index_intraday", "index_kline", "index_full_info", "l2_realtime_index_list"],
        "fallback": ["daily_summary", "market_rise_fall", "advanced_market_scln"],
        "why": "判断指数系统性风险和权重/题材跷跷板。",
    },
    {
        "chain": "L2可用数据",
        "required": ["l2_realtime_all_boards", "l2_sector_intraday"],
        "fallback": ["ladder_realtime_boards"],
        "why": "盘中涨停、板块分时和急跌风险监控。",
    },
    {
        "chain": "市场状态",
        "required": ["v_market_state_inputs", "v_market_daily", "daily_summary", "market_rise_fall", "market_emotion_money"],
        "fallback": ["market_mood", "daily_sentiment", "daily_new_high"],
        "why": "情绪周期、赚钱效应、涨跌停和风险温度。",
    },
]


def _table_or_view_names(con: duckdb.DuckDBPyConnection) -> set[str]:
    tables = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    views = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'main'"
        ).fetchall()
    }
    return tables | views


def _object_row_counts(
    con: duckdb.DuckDBPyConnection,
    names: set[str],
    trade_date: str | None = None,
) -> tuple[dict[str, int], dict[str, str | None]]:
    counts = {}
    latest_dates = {}
    for name in names:
        try:
            columns = set(table_columns(con, name))
            date_column = next(
                (column for column in ("trade_date", "date", "source_date") if column in columns),
                None,
            )
            if date_column:
                latest = con.execute(
                    f'SELECT max(CAST("{date_column}" AS VARCHAR)) FROM "{name}"'
                ).fetchone()[0]
                latest_dates[name] = str(latest) if latest is not None else None
            else:
                latest_dates[name] = None
            if trade_date and date_column:
                counts[name] = con.execute(
                    f'SELECT count(*) FROM "{name}" WHERE CAST("{date_column}" AS VARCHAR) = ?',
                    [trade_date],
                ).fetchone()[0]
            else:
                counts[name] = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except Exception:
            counts[name] = 0
            latest_dates[name] = None
    return counts, latest_dates


def assess_data_chains(
    db_path: str | Path,
    trade_date: str | None = None,
    con: "duckdb.DuckDBPyConnection | None" = None,
) -> list[dict]:
    # Reuse an existing connection when given: DuckDB forbids two
    # connections to the same file with different configurations in one
    # process, so opening read_only here would fail next to a live
    # read-write connection (e.g. build_terminal_context).
    owns_connection = con is None
    if owns_connection:
        if connect_duckdb is not None:
            con = connect_duckdb(str(db_path), read_only=True)
        else:
            con = duckdb.connect(str(db_path), read_only=True)
    try:
        existing = _table_or_view_names(con)
        row_counts, latest_dates = _object_row_counts(con, existing, trade_date)
    finally:
        if owns_connection:
            con.close()

    results = []
    for chain in DATA_CHAINS:
        required_present = [name for name in chain["required"] if row_counts.get(name, 0) > 0]
        fallback_present = [name for name in chain["fallback"] if row_counts.get(name, 0) > 0]
        required_existing_empty = [
            name for name in chain["required"] if name in existing and row_counts.get(name, 0) == 0
        ]
        # Same-date stale: relation has history but zero rows for trade_date.
        if trade_date and not required_present and not fallback_present:
            any_history = any(
                latest_dates.get(name)
                for name in chain["required"] + chain["fallback"]
            )
            if any_history:
                status = "stale"
            else:
                status = "missing"
        elif required_present:
            status = "available"
            # If caller asked for a trade date and only older dates exist on
            # required sources, mark stale even when total row counts are >0
            # (when trade_date filter already applied, required_present is empty).
            if trade_date:
                same_day = [
                    name
                    for name in required_present
                    if (latest_dates.get(name) or "")[:10] >= str(trade_date)[:10]
                    or row_counts.get(name, 0) > 0
                ]
                # row_counts already scoped to trade_date when provided.
                if not same_day and not any(row_counts.get(n, 0) > 0 for n in required_present):
                    status = "stale"
        elif fallback_present:
            status = "fallback"
        elif trade_date and any(latest_dates.get(name) for name in chain["required"] + chain["fallback"]):
            status = "stale"
        else:
            status = "missing"
        results.append(
            {
                "chain": chain["chain"],
                "status": status,
                "required": chain["required"],
                "required_present": required_present,
                "required_empty": required_existing_empty,
                "required_missing": [name for name in chain["required"] if name not in existing],
                "fallback_present": fallback_present,
                "row_counts": {name: row_counts.get(name, 0) for name in chain["required"] + chain["fallback"] if name in existing},
                "latest_dates": {name: latest_dates.get(name) for name in chain["required"] + chain["fallback"] if name in existing},
                "trade_date": trade_date,
                "why": chain["why"],
            }
        )
    return results


def render_data_chain_markdown(chains: list[dict]) -> str:
    trade_date = next((item.get("trade_date") for item in chains if item.get("trade_date")), None)
    lines = [
        "# Trading Data Chain Status",
        "",
        f"- Trade date: `{trade_date}`" if trade_date else "- Trade date: `all available history`",
        "",
        "| Chain | Status | Present | Missing Required | Why It Matters |",
        "|---|---|---|---|---|",
    ]
    for item in chains:
        present = ", ".join(item["required_present"] or item["fallback_present"])
        missing = ", ".join(item["required_missing"])
        lines.append(f"| {item['chain']} | {item['status']} | {present} | {missing} | {item['why']} |")
    lines.append("")
    return "\n".join(lines)
