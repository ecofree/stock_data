"""Lightweight backtest/statistical checks for available market data."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import duckdb

from trade_system.quality import table_columns, table_exists
from trade_system.signals import classify_market_regime
from trade_system.db_utils import fetch_dicts as _fetch_dicts


def run_market_regime_backtest(db_path: str | Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if not table_exists(con, "daily_summary"):
            return {"sample_count": 0, "regime_counts": {}, "rows": []}
        rows = con.execute(
            """
            SELECT date, limit_up_count, limit_down_count, rise_count, fall_count, consecutive_count
            FROM (
                SELECT date, limit_up_count, limit_down_count, rise_count, fall_count,
                       consecutive_count, fetched_at,
                       row_number() OVER (PARTITION BY date ORDER BY fetched_at DESC NULLS LAST) AS rn
                FROM daily_summary
            )
            WHERE rn = 1
            ORDER BY date
            """
        ).fetchall()
        classified = []
        counts = Counter()
        for date, limit_up, limit_down, rise, fall, consecutive in rows:
            regime = classify_market_regime(
                {
                    "limit_up_count": limit_up,
                    "limit_down_count": limit_down,
                    "rise_count": rise,
                    "fall_count": fall,
                    "consecutive_count": consecutive,
                }
            )
            item = {
                "trade_date": str(date),
                "regime": regime["regime"],
                "suggested_position_pct": regime["suggested_position_pct"],
                "limit_up_count": limit_up,
                "limit_down_count": limit_down,
            }
            counts[regime["regime"]] += 1
            classified.append(item)
        return {
            "sample_count": len(classified),
            "regime_counts": dict(counts),
            "rows": classified,
        }
    finally:
        con.close()



def run_stage_candidate_backtest(db_path: str | Path, *, enforce_t1: bool = False) -> dict:
    """Backtest actionable stage-v2 signals with stage-appropriate prices.

    A-share T+1 is enforced: an entry cannot be sold until the next trading
    session.  Close decisions enter next-session open and exit the following
    session close; premarket/auction/intraday entries exit next-session close.
    Production tables with ``is_actionable`` never admit legacy/null rows into
    the statistics.
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if not table_exists(con, "stock_candidate_stage_signal"):
            return {"sample_count": 0, "stage_counts": {}, "stage_stats": {}, "rows": []}
        signal_columns = set(table_columns(con, "stock_candidate_stage_signal"))
        actionability = (
            "WHERE coalesce(is_actionable, false) = true"
            if "is_actionable" in signal_columns
            else ""
        )
        reference_select = (
            "reference_price, reference_price_type, as_of_time, feature_version"
            if "reference_price" in signal_columns
            else "NULL AS reference_price, NULL AS reference_price_type, "
            "NULL AS as_of_time, NULL AS feature_version"
        )
        decision_select = "decision" if "decision" in signal_columns else "NULL AS decision"
        signals = _fetch_dicts(
            con,
            f"""
            SELECT trade_date, stage, stock_code, stock_name, score, {decision_select},
                   {reference_select}
            FROM stock_candidate_stage_signal
            {actionability}
            ORDER BY trade_date, stock_code, stage
            """,
        )
        if not signals:
            return {"sample_count": 0, "stage_counts": {}, "stage_stats": {}, "rows": []}
        # Never scan the complete daily-history relation for a candidate
        # backtest.  The production database contains millions of rows while
        # this report only needs the stocks present in the signal set.  The
        # old unbounded fetchall() was the direct cause of MemoryError after
        # several days of accumulated history.
        stock_codes = sorted({str(row.get("stock_code")) for row in signals if row.get("stock_code") is not None})
        if not stock_codes:
            return {
                "sample_count": len(signals),
                "stage_counts": {},
                "stage_stats": {},
                "rows": [],
                "excluded_count": len(signals),
            }
        code_placeholders = ",".join("?" for _ in stock_codes)
        if table_exists(con, "v_kline_daily"):
            kline_rows = _fetch_dicts(
                con,
                f"""
                SELECT trade_date, stock_code, open, close
                FROM v_kline_daily
                WHERE close IS NOT NULL
                  AND CAST(stock_code AS VARCHAR) IN ({code_placeholders})
                ORDER BY stock_code, trade_date
                """,
                stock_codes,
            )
        elif table_exists(con, "kline"):
            kline_columns = set(table_columns(con, "kline"))
            open_expr = "open" if "open" in kline_columns else "NULL AS open"
            ktype_filter = (
                "AND upper(coalesce(ktype, 'D')) = 'D'" if "ktype" in kline_columns else ""
            )
            order_expr = "fetched_at DESC NULLS LAST" if "fetched_at" in kline_columns else "rowid DESC"
            kline_rows = _fetch_dicts(
                con,
                f"""
                SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, {open_expr}, close
                FROM kline
                WHERE close IS NOT NULL {ktype_filter}
                  AND CAST(stock_code AS VARCHAR) IN ({code_placeholders})
                QUALIFY row_number() OVER (
                    PARTITION BY date, stock_code
                    ORDER BY {order_expr}
                ) = 1
                ORDER BY stock_code, date
                """,
                stock_codes,
            )
        else:
            kline_rows = []
    finally:
        con.close()

    by_stock: dict[str, list[dict]] = {}
    for row in kline_rows:
        by_stock.setdefault(row["stock_code"], []).append(row)

    output_rows = []
    stage_counts: Counter = Counter()
    forward_returns: dict[str, list[float]] = {}
    excluded_count = 0
    for signal in signals:
        stage = signal["stage"]
        stage_counts[stage] += 1
        entry_price = None
        exit_price = None
        entry_date = None
        exit_date = None
        return_method = None
        rows = by_stock.get(signal["stock_code"], [])
        for index, kline in enumerate(rows):
            if kline["trade_date"] == signal["trade_date"]:
                if stage in {"premarket_pool", "auction_confirmation"} and (not enforce_t1 or index + 1 < len(rows)):
                    entry_date = kline["trade_date"]
                    exit_date = rows[index + 1]["trade_date"] if enforce_t1 else kline["trade_date"]
                    entry_price = kline.get("open")
                    exit_price = rows[index + 1].get("close") if enforce_t1 else kline.get("close")
                    return_method = "same_day_open_to_next_day_close_t1" if enforce_t1 else "same_day_open_to_close_compatibility"
                elif stage == "close_decision" and (index + 2 < len(rows) if enforce_t1 else index + 1 < len(rows)):
                    next_row = rows[index + 1]
                    exit_row = rows[index + 2] if enforce_t1 else next_row
                    entry_date = next_row["trade_date"]
                    exit_date = exit_row["trade_date"]
                    entry_price = next_row.get("open")
                    exit_price = exit_row.get("close")
                    return_method = "next_day_open_to_following_day_close_t1" if enforce_t1 else "next_day_open_to_close_compatibility"
                elif stage == "intraday_strength" and signal.get("reference_price") and (not enforce_t1 or index + 1 < len(rows)):
                    entry_date = kline["trade_date"]
                    exit_date = rows[index + 1]["trade_date"] if enforce_t1 else kline["trade_date"]
                    entry_price = signal.get("reference_price")
                    exit_price = rows[index + 1].get("close") if enforce_t1 else kline.get("close")
                    return_method = "captured_intraday_price_to_next_day_close_t1" if enforce_t1 else "captured_intraday_price_to_close_compatibility"
                break
        forward_return = None
        if entry_price not in (None, 0) and exit_price is not None:
            forward_return = round(
                (float(exit_price) - float(entry_price)) * 100.0 / float(entry_price), 2
            )
            forward_returns.setdefault(stage, []).append(forward_return)
        else:
            excluded_count += 1
        output_rows.append(
            {
                **signal,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return_method": return_method,
                "forward_return_pct": forward_return,
                "hit": forward_return is not None and forward_return > 0,
            }
        )

    stage_stats = {}
    for stage, count in stage_counts.items():
        returns = forward_returns.get(stage, [])
        hit_count = sum(1 for value in returns if value > 0)
        stage_stats[stage] = {
            "sample_count": count,
            "return_sample_count": len(returns),
            "hit_count": hit_count,
            "hit_rate": round(hit_count * 100.0 / len(returns), 2) if returns else None,
            "avg_forward_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        }

    return {
        "sample_count": len(output_rows),
        "return_sample_count": sum(len(values) for values in forward_returns.values()),
        "independent_sample_count": len(
            {
                (row["trade_date"], row["stock_code"])
                for row in output_rows
                if row["forward_return_pct"] is not None
            }
        ),
        "excluded_count": excluded_count,
        "stage_counts": dict(stage_counts),
        "stage_stats": stage_stats,
        "rows": output_rows,
    }


def render_backtest_markdown(result: dict) -> str:
    lines = [
        "# Market Regime Backtest",
        "",
        f"- Sample count: {result['sample_count']}",
        f"- Return samples: {result.get('return_sample_count', 0)}",
        f"- Independent stock-date samples: {result.get('independent_sample_count', 0)}",
        f"- Excluded for non-executable price: {result.get('excluded_count', 0)}",
        "",
        "## Regime Counts",
        "",
        "| Regime | Count |",
        "|---|---:|",
    ]
    for regime, count in sorted(result["regime_counts"].items()):
        lines.append(f"| {regime} | {count} |")
    lines.extend(["", "## Latest Samples", "", "| Date | Regime | Position | Limit Up | Limit Down |", "|---|---|---:|---:|---:|"])
    for item in result["rows"][-20:]:
        lines.append(
            f"| {item['trade_date']} | {item['regime']} | {item['suggested_position_pct']} | "
            f"{item['limit_up_count']} | {item['limit_down_count']} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_stage_backtest_markdown(result: dict) -> str:
    lines = [
        "# Stage Candidate Backtest",
        "",
        f"- Sample count: {result['sample_count']}",
        "",
        "| Stage | Signals | Return Samples | Hit Rate | Avg Forward Return |",
        "|---|---:|---:|---:|---:|",
    ]
    for stage, stats in sorted(result.get("stage_stats", {}).items()):
        hit_rate = "" if stats["hit_rate"] is None else f"{stats['hit_rate']:.2f}%"
        avg_return = "" if stats["avg_forward_return_pct"] is None else f"{stats['avg_forward_return_pct']:.2f}%"
        lines.append(
            f"| {stage} | {stats['sample_count']} | {stats['return_sample_count']} | "
            f"{hit_rate} | {avg_return} |"
        )
    lines.extend(["", "## Latest Samples", "", "| Date | Stage | Stock | Score | Forward Return |", "|---|---|---|---:|---:|"])
    for item in result.get("rows", [])[-30:]:
        fwd = "" if item["forward_return_pct"] is None else f"{item['forward_return_pct']:.2f}%"
        lines.append(
            f"| {item['trade_date']} | {item['stage']} | {item['stock_name'] or item['stock_code']} | "
            f"{float(item['score'] or 0):.1f} | {fwd} |"
        )
    lines.append("")
    return "\n".join(lines)
