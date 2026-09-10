"""Status report for real-data backfill progress."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.quality import table_columns, table_exists


CORE_RELATIONS = [
    "auction_tick",
    "auction_bidding_anomaly",
    "advanced_morning_bidding_summary",
    "sector_capital",
    "kline",
    "index_kline",
    "l2_realtime_index_list",
]


def _count(con: duckdb.DuckDBPyConnection, relation: str) -> int:
    if not table_exists(con, relation):
        return 0
    return int(con.execute(f'SELECT count(*) FROM "{relation}"').fetchone()[0])


def _distinct_dates(con: duckdb.DuckDBPyConnection, relation: str) -> int:
    if not table_exists(con, relation):
        return 0
    cols = table_columns(con, relation)
    date_col = next((col for col in ("date", "trade_date") if col in cols), None)
    if not date_col:
        return 0
    return int(con.execute(f'SELECT count(DISTINCT CAST("{date_col}" AS VARCHAR)) FROM "{relation}"').fetchone()[0])


def _required_days(con: duckdb.DuckDBPyConnection) -> int:
    return _distinct_dates(con, "daily_summary") or 250


def _coverage_item(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    required_days: int,
    source: str,
    missing_status: str = "needs_external_source",
) -> dict:
    rows = _count(con, relation)
    observed_days = _distinct_dates(con, relation)
    if rows == 0:
        status = missing_status
    elif observed_days >= required_days:
        status = "ok"
    else:
        status = "needs_backfill"
    return {
        "relation": relation,
        "rows": rows,
        "observed_days": observed_days,
        "required_days": required_days,
        "status": status,
        "source": source,
    }


def build_real_data_backfill_status(db_path: str | Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {relation: _count(con, relation) for relation in CORE_RELATIONS}
        required_days = _required_days(con)
        history_coverage = {
            "kline": _coverage_item(con, "kline", required_days, "/kline or CSV backfill"),
            "index_kline": _coverage_item(con, "index_kline", required_days, "/index/zhishu-kline"),
            "index_intraday": _coverage_item(con, "index_intraday", 1, "/index/intraday", "needs_trading_session"),
            "sector_strength": _coverage_item(con, "sector_strength", required_days, "/sector/strength-history", "needs_external_source"),
            "sector_stocks": _coverage_item(con, "sector_stocks", required_days, "/sector/stocks", "needs_external_source"),
            "sector_capital": _coverage_item(con, "sector_capital", required_days, "/sector/capital"),
            "limit_pool": _coverage_item(con, "l2_realtime_all_boards", required_days, "/l2/realtime/all-boards", "needs_trading_session"),
            "auction_tick": _coverage_item(con, "auction_tick", 1, "/auction/tick", "needs_trading_session"),
        }
    finally:
        con.close()

    gaps = []
    if counts["auction_tick"] == 0:
        gaps.append("auction_tick remains empty; do not synthesize auction ticks.")
    if counts["index_kline"] == 0:
        gaps.append("index_kline remains empty; run index collection or derive fallback from daily index fields.")
    if counts["kline"] < 100:
        gaps.append("kline sample is still thin for professional staged backtests.")
    if counts["sector_capital"] == 0:
        gaps.append("sector_capital is empty; sector money-flow continuity is not available.")
    for item in history_coverage.values():
        if item["status"] != "ok":
            gaps.append(
                f"{item['relation']} history status={item['status']} "
                f"observed_days={item['observed_days']}/{item['required_days']} source={item['source']}."
            )
    return {"counts": counts, "history_coverage": history_coverage, "gaps": gaps}


def render_real_data_backfill_report(status: dict) -> str:
    lines = [
        "# Real Data Backfill Status",
        "",
        "| Relation | Rows |",
        "|---|---:|",
    ]
    for name, count in status["counts"].items():
        lines.append(f"| `{name}` | {count} |")

    lines.extend(
        [
            "",
            "## History Coverage",
            "",
            "| Relation | Rows | Observed Days | Required Days | Status | Source |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for item in status.get("history_coverage", {}).values():
        lines.append(
            f"| `{item['relation']}` | {item['rows']} | {item['observed_days']} | "
            f"{item['required_days']} | `{item['status']}` | {item['source']} |"
        )

    lines.extend(["", "## Open Gaps", ""])
    if status["gaps"]:
        for gap in status["gaps"]:
            lines.append(f"- {gap}")
    else:
        lines.append("- No critical data gaps detected by this bounded check.")

    lines.extend(
        [
            "",
            "## Operator Interpretation",
            "",
            "- Historical backfill is for signal validation and sample statistics only.",
            "- Missing auction ticks must remain an explicit gap; do not synthesize them.",
            "- When a relation is marked `needs_external_source`, use CSV/import adapters or a second market-data provider.",
            "- This report never creates automatic trading instructions.",
            "",
        ]
    )
    return "\n".join(lines)
