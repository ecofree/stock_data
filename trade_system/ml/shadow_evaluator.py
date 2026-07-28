"""Evaluate qlib shadow predictions against next available daily K-line close."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import mean

import duckdb

from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables
from trade_system.quality import table_exists


def _fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def evaluate_qlib_shadow(db_path: str | Path) -> dict:
    ensure_qlib_shadow_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        predictions = _fetch_dicts(
            con,
            """
            SELECT trade_date, symbol, model_id, score, rank, horizon
            FROM qlib_prediction
            ORDER BY model_id, trade_date, rank
            """,
        )
        if table_exists(con, "v_kline_daily"):
            kline_rows = _fetch_dicts(
                con,
                """
                SELECT trade_date, stock_code, close
                FROM v_kline_daily
                WHERE close IS NOT NULL
                ORDER BY stock_code, trade_date
                """,
            )
        elif table_exists(con, "kline"):
            kline_rows = _fetch_dicts(
                con,
                """
                SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, close
                FROM kline
                WHERE close IS NOT NULL
                  AND (ktype IS NULL OR ktype = 'D')
                ORDER BY stock_code, date
                """,
            )
        else:
            kline_rows = []
    finally:
        con.close()

    by_stock: dict[str, list[dict]] = defaultdict(list)
    for row in kline_rows:
        by_stock[str(row["stock_code"])].append(row)

    evaluated_rows = []
    for prediction in predictions:
        series = by_stock.get(str(prediction["symbol"]), [])
        for index, kline in enumerate(series):
            if str(kline["trade_date"]) == str(prediction["trade_date"]) and index + 1 < len(series):
                current_close = float(kline["close"])
                next_close = float(series[index + 1]["close"])
                forward_return = (next_close - current_close) * 100.0 / current_close
                evaluated_rows.append({**prediction, "forward_return_pct": round(forward_return, 2)})
                break

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in evaluated_rows:
        grouped[row["model_id"]].append(row)

    con = duckdb.connect(str(db_path))
    try:
        con.execute("DELETE FROM qlib_shadow_evaluation")
        models = {}
        for model_id, rows in sorted(grouped.items()):
            returns = [float(row["forward_return_pct"]) for row in rows]
            ranked = sorted(rows, key=lambda item: float(item.get("rank") or 999999))
            split = max(1, len(ranked) // 2)
            top_returns = [float(row["forward_return_pct"]) for row in ranked[:split]]
            bottom_returns = [float(row["forward_return_pct"]) for row in ranked[-split:]]
            dates = sorted(str(row["trade_date"]) for row in rows)
            hit_rate = round(sum(1 for value in returns if value > 0) * 100.0 / len(returns), 2) if returns else None
            item = {
                "sample_count": len(rows),
                "hit_rate": hit_rate,
                "avg_return": round(mean(returns), 2) if returns else None,
                "top_quantile_return": round(mean(top_returns), 2) if top_returns else None,
                "bottom_quantile_return": round(mean(bottom_returns), 2) if bottom_returns else None,
                "max_drawdown": min(returns) if returns else None,
            }
            models[model_id] = item
            con.execute(
                """
                INSERT INTO qlib_shadow_evaluation (
                    model_id, sample_start, sample_end, sample_count, ic, rank_ic,
                    top_quantile_return, bottom_quantile_return, hit_rate, max_drawdown
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    model_id,
                    dates[0] if dates else "",
                    dates[-1] if dates else "",
                    len(rows),
                    None,
                    None,
                    item["top_quantile_return"],
                    item["bottom_quantile_return"],
                    item["hit_rate"],
                    item["max_drawdown"],
                ],
            )
        return {"sample_count": len(evaluated_rows), "models": models, "rows": evaluated_rows}
    finally:
        con.close()
