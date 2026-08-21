"""Backtest strategy scan results through validated stage execution rules."""

from __future__ import annotations

from pathlib import Path
from collections import defaultdict
from statistics import mean

import duckdb

from trade_system.backtest import run_stage_candidate_backtest
from trade_system.quality import table_exists
from trade_system.db_utils import fetch_dicts as _fetch_dicts



def run_strategy_result_backtest(
    db_path: str | Path,
    fee_rate: float = 0.001,
    slippage_bps: float = 10.0,
    *,
    enforce_t1: bool = False,
) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if not table_exists(con, "strategy_scan_result"):
            return {"sample_count": 0, "rows": [], "summary": {}}
        signals = _fetch_dicts(
            con,
            """
            SELECT trade_date, strategy_id, symbol, stock_name, stage, score
            FROM strategy_scan_result
            ORDER BY trade_date, symbol, strategy_id
            """,
        )
    finally:
        con.close()

    stage_result = run_stage_candidate_backtest(db_path, enforce_t1=enforce_t1)
    stage_aliases = {
        "premarket_pool": "pre_market",
        "auction_confirmation": "auction_confirm",
        "intraday_strength": "intraday_strength",
        "close_decision": "closing_decision",
    }
    stage_rows = {
        (str(row["trade_date"]), stage_aliases.get(row["stage"], row["stage"]), str(row["stock_code"])): row
        for row in stage_result.get("rows", [])
        if row.get("forward_return_pct") is not None
    }
    rows = []
    cost_pct = fee_rate * 100.0 + slippage_bps / 100.0
    for signal in signals:
        stage_row = stage_rows.get(
            (str(signal["trade_date"]), str(signal["stage"]), str(signal["symbol"]))
        )
        if not stage_row:
            continue
        gross = float(stage_row["forward_return_pct"])
        rows.append(
            {
                **signal,
                "entry_date": stage_row.get("entry_date"),
                "exit_date": stage_row.get("exit_date"),
                "signal_close": round(float(stage_row.get("entry_price") or 0), 4),
                "t1_close": round(float(stage_row.get("exit_price") or 0), 4),
                "gross_return_pct": round(gross, 2),
                "net_return_pct": round(gross - cost_pct, 2),
                "return_method": stage_row.get("return_method"),
            }
        )

    returns = [row["net_return_pct"] for row in rows]
    summary = {
        "avg_net_return_pct": round(mean(returns), 2) if returns else None,
        "win_rate_pct": round(sum(1 for value in returns if value > 0) * 100.0 / len(returns), 2) if returns else None,
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "independent_sample_count": len(
            {(row["trade_date"], row["symbol"]) for row in rows}
        ),
    }
    return {"sample_count": len(rows), "rows": rows, "summary": summary}


def render_strategy_backtest_markdown(result: dict) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Strategy Result Backtest",
        "",
        f"- Sample count: {result.get('sample_count', 0)}",
        f"- Average net return: {summary.get('avg_net_return_pct')}",
        f"- Win rate: {summary.get('win_rate_pct')}",
        f"- Independent stock-date samples: {summary.get('independent_sample_count', 0)}",
        "",
        "| Signal Date | Entry Date | Strategy | Stage | Symbol | Score | Net % |",
        "|---|---|---|---|---|---:|---:|",
    ]
    for row in result.get("rows", [])[:100]:
        lines.append(
            f"| {row['trade_date']} | {row['entry_date']} | {row['strategy_id']} | {row['stage']} | "
            f"{row['symbol']} | {float(row['score'] or 0):.2f} | {row['net_return_pct']:.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def summarize_strategy_backtest(result: dict, config_hash: str = "default") -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in result.get("rows", []):
        grouped[(row["strategy_id"], row["stage"])].append(row)

    summaries = []
    for (strategy_id, stage), rows in sorted(grouped.items()):
        returns = [float(row["net_return_pct"]) for row in rows]
        dates = sorted(str(row["trade_date"]) for row in rows)
        summaries.append(
            {
                "strategy_id": strategy_id,
                "stage": stage,
                "sample_start": dates[0] if dates else "",
                "sample_end": dates[-1] if dates else "",
                "sample_count": len(rows),
                "win_rate": round(sum(1 for value in returns if value > 0) * 100.0 / len(returns), 2)
                if returns
                else None,
                "avg_return": round(mean(returns), 2) if returns else None,
                "max_drawdown": min(returns) if returns else None,
                "profit_factor": None,
                "config_hash": config_hash,
            }
        )
    return summaries
