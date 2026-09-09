"""Evaluate qlib shadow predictions against next available daily K-line close."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from math import ceil
import re

import duckdb

from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables
from trade_system.quality import table_exists
from trade_system.db_utils import fetch_dicts as _fetch_dicts



def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rank(values: list[float]) -> list[float]:
    """Average ranks with ties, implemented without a scipy dependency."""
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = (index + 1 + end) / 2.0
        for position in range(index, end):
            result[ordered[position][0]] = average
        index = end
    return result


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return numerator / (left_var * right_var) ** 0.5


def _horizon_bars(value: str | None) -> tuple[int, bool]:
    text = str(value or "t1").lower()
    match = re.search(r"t(\d+)", text)
    bars = max(1, int(match.group(1))) if match else 1
    return bars, text.endswith("_exec")


def _max_drawdown(returns: list[float]) -> float | None:
    if not returns:
        return None
    equity = 1.0
    peak = equity
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity / peak - 1.0) * 100.0)
    return worst


def evaluate_qlib_shadow(
    db_path: str | Path,
    *,
    quantile: float = 0.2,
    round_trip_cost_bps: float = 25.0,
) -> dict:
    """Evaluate shadow predictions with an optional conservative round-trip cost.

    The default is cost-aware (25 bps) so displayed results cannot be mistaken
    for executable performance. Pass 0.0 explicitly for legacy gross-only runs.
    """
    round_trip_cost_bps = max(0.0, float(round_trip_cost_bps))
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
        prediction_symbols = sorted({str(row.get("symbol")) for row in predictions if row.get("symbol") is not None})
        symbol_placeholders = ",".join("?" for _ in prediction_symbols)
        if table_exists(con, "v_kline_daily"):
            kline_cols = {str(row[1]) for row in con.execute("PRAGMA table_info('v_kline_daily')").fetchall()}
            open_expr = "open" if "open" in kline_cols else "NULL"
            kline_rows = _fetch_dicts(
                con,
                f"""
                SELECT trade_date, stock_code, {open_expr} AS open, close
                FROM v_kline_daily
                WHERE close IS NOT NULL
                  AND CAST(stock_code AS VARCHAR) IN ({symbol_placeholders or "NULL"})
                ORDER BY stock_code, trade_date
                """,
                prediction_symbols,
            )
        elif table_exists(con, "kline"):
            kline_cols = {str(row[1]) for row in con.execute("PRAGMA table_info('kline')").fetchall()}
            open_expr = "open" if "open" in kline_cols else "NULL"
            kline_rows = _fetch_dicts(
                con,
                f"""
                SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, {open_expr} AS open, close
                FROM kline
                WHERE close IS NOT NULL
                  AND (ktype IS NULL OR ktype = 'D')
                  AND CAST(stock_code AS VARCHAR) IN ({symbol_placeholders or "NULL"})
                ORDER BY stock_code, date
                """,
                prediction_symbols,
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
        bars, execution_aware = _horizon_bars(prediction.get("horizon"))
        for index, kline in enumerate(series):
            if str(kline["trade_date"]) != str(prediction["trade_date"]):
                continue
            if execution_aware:
                buy_index = index + 1
                sell_index = buy_index + bars
                if sell_index >= len(series) or series[buy_index].get("open") in (None, 0):
                    break
                buy_price = float(series[buy_index]["open"])
                sell_price = float(series[sell_index]["close"])
                label_mode = "next_open_to_t_plus_n_close_t1_compliant"
            else:
                sell_index = index + bars
                if sell_index >= len(series):
                    break
                buy_price = float(kline["close"])
                sell_price = float(series[sell_index]["close"])
                label_mode = "close_to_future_close_legacy"
            if buy_price > 0:
                forward_return = (sell_price - buy_price) * 100.0 / buy_price
                evaluated_rows.append({
                    **prediction,
                    "forward_return_pct": round(forward_return, 4),
                    "net_forward_return_pct": round(forward_return - round_trip_cost_bps / 100.0, 4),
                    "forward_date": str(series[sell_index]["trade_date"]),
                    "label_mode": label_mode,
                })
                break

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in evaluated_rows:
        grouped[row["model_id"]].append(row)

    con = duckdb.connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        # 追加式版本化：只覆盖同一 (model, method, quantile, cost) 的旧行，
        # 不删其他参数/模型的历史评估，保留追溯链。
        con.execute(
            "ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS round_trip_cost_bps DOUBLE"
        )
        con.execute(
            "DELETE FROM qlib_shadow_evaluation WHERE evaluation_method = 'daily_cross_sectional_quantile' AND quantile = ? AND coalesce(round_trip_cost_bps, 0.0) = ?",
            [quantile, round_trip_cost_bps],
        )
        models = {}
        for model_id, rows in sorted(grouped.items()):
            returns = [float(row["forward_return_pct"]) for row in rows]
            net_returns = [float(row["net_forward_return_pct"]) for row in rows]
            by_date: dict[str, list[dict]] = defaultdict(list)
            for row in rows:
                by_date[str(row["trade_date"])].append(row)
            top_returns: list[float] = []
            bottom_returns: list[float] = []
            daily_top_returns: list[float] = []
            daily_bottom_returns: list[float] = []
            daily_top_net_returns: list[float] = []
            daily_bottom_net_returns: list[float] = []
            daily_ics: list[float] = []
            daily_rank_ics: list[float] = []
            for trade_date, day_rows in sorted(by_date.items()):
                ordered = sorted(
                    day_rows,
                    key=lambda item: (-float(item.get("score") or 0), int(item.get("rank") or 999999), str(item.get("symbol"))),
                )
                split = max(1, ceil(len(ordered) * max(0.01, min(0.5, quantile))))
                top = ordered[:split]
                bottom = ordered[-split:]
                top_day = [float(row["forward_return_pct"]) for row in top]
                bottom_day = [float(row["forward_return_pct"]) for row in bottom]
                top_net_day = [float(row["net_forward_return_pct"]) for row in top]
                bottom_net_day = [float(row["net_forward_return_pct"]) for row in bottom]
                top_returns.extend(top_day)
                bottom_returns.extend(bottom_day)
                daily_top_returns.append(sum(top_day) / len(top_day))
                daily_bottom_returns.append(sum(bottom_day) / len(bottom_day))
                daily_top_net_returns.append(sum(top_net_day) / len(top_net_day))
                daily_bottom_net_returns.append(sum(bottom_net_day) / len(bottom_net_day))
                scores = [float(row.get("score") or 0) for row in day_rows]
                day_returns = [float(row["forward_return_pct"]) for row in day_rows]
                ic = _correlation(scores, day_returns)
                rank_ic = _correlation(_rank(scores), _rank(day_returns))
                if ic is not None:
                    daily_ics.append(ic)
                if rank_ic is not None:
                    daily_rank_ics.append(rank_ic)
            dates = sorted(str(row["trade_date"]) for row in rows)
            hit_rate = round(sum(1 for value in returns if value > 0) * 100.0 / len(returns), 2) if returns else None
            top_hit_rate = round(sum(1 for value in top_returns if value > 0) * 100.0 / len(top_returns), 2) if top_returns else None
            daily_top_hit_rate = round(sum(1 for value in daily_top_returns if value > 0) * 100.0 / len(daily_top_returns), 2) if daily_top_returns else None
            item = {
                "sample_count": len(rows),
                "hit_rate": hit_rate,
                "avg_return": round(_mean(returns), 4) if returns else None,
                "avg_forward_return": round(_mean(returns), 4) if returns else None,
                "avg_net_return": round(_mean(net_returns), 4) if net_returns else None,
                "top_quantile_return": round(_mean(daily_top_returns), 4) if daily_top_returns else None,
                "bottom_quantile_return": round(_mean(daily_bottom_returns), 4) if daily_bottom_returns else None,
                "top_bottom_spread": round(_mean([a - b for a, b in zip(daily_top_returns, daily_bottom_returns)]), 4) if daily_top_returns else None,
                "top_quantile_net_return": round(_mean(daily_top_net_returns), 4) if daily_top_net_returns else None,
                "bottom_quantile_net_return": round(_mean(daily_bottom_net_returns), 4) if daily_bottom_net_returns else None,
                "net_top_bottom_spread": round(_mean([a - b for a, b in zip(daily_top_net_returns, daily_bottom_net_returns)]), 4) if daily_top_net_returns else None,
                "top_hit_rate": top_hit_rate,
                "daily_top_hit_rate": daily_top_hit_rate,
                "ic": round(_mean(daily_ics), 6) if daily_ics else None,
                "rank_ic": round(_mean(daily_rank_ics), 6) if daily_rank_ics else None,
                "max_drawdown": round(_max_drawdown(daily_top_net_returns), 4) if daily_top_net_returns else None,
                "evaluation_method": "daily_cross_sectional_quantile",
                "quantile": quantile,
                "round_trip_cost_bps": round_trip_cost_bps,
            }
            models[model_id] = item
            con.execute(
                """
                INSERT INTO qlib_shadow_evaluation (
                    model_id, sample_start, sample_end, sample_count, ic, rank_ic,
                    avg_forward_return, top_quantile_return, bottom_quantile_return,
                    top_bottom_spread, hit_rate, top_hit_rate, daily_top_hit_rate,
                    max_drawdown, evaluation_method, quantile, round_trip_cost_bps, updated_at,
                    avg_net_return, net_top_bottom_spread, top_quantile_net_return,
                    bottom_quantile_net_return
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    model_id,
                    dates[0] if dates else "",
                    dates[-1] if dates else "",
                    len(rows),
                    item["ic"],
                    item["rank_ic"],
                    item["avg_forward_return"],
                    item["top_quantile_return"],
                    item["bottom_quantile_return"],
                    item["top_bottom_spread"],
                    item["hit_rate"],
                    item["top_hit_rate"],
                    item["daily_top_hit_rate"],
                    item["max_drawdown"],
                    item["evaluation_method"],
                    item["quantile"],
                    round_trip_cost_bps,
                    datetime.now(),
                    item["avg_net_return"],
                    item["net_top_bottom_spread"],
                    item["top_quantile_net_return"],
                    item["bottom_quantile_net_return"],
                ],
            )
        result = {"sample_count": len(evaluated_rows), "models": models, "rows": evaluated_rows}
        con.commit()
        return result
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()
