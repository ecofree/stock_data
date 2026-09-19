"""Read-only historical shadow diagnostics on verified exchange sessions."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from math import ceil, isfinite
import re


from trade_system.quality import table_exists
from trade_system.trading_calendar import open_session_dates
from trade_system.review_metrics import average_ranks, correlation
from trade_system.db_utils import fetch_dicts as _fetch_dicts



def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _horizon_bars(value: str | None) -> tuple[int, bool]:
    match = re.fullmatch(r"t([1-9][0-9]?)(_exec)?", str(value or ""))
    if match is None:
        raise ValueError("explicit t1..t99 or t1_exec..t99_exec horizon required")
    return int(match[1]), bool(match[2])


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
    if (isinstance(quantile, bool) or not isfinite(quantile) or not 0 < quantile <= .5
            or isinstance(round_trip_cost_bps, bool) or not isfinite(round_trip_cost_bps)
            or round_trip_cost_bps < 0):
        raise ValueError("finite quantile in (0,.5] and nonnegative cost required")
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path), read_only=True)
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
                WHERE CAST(stock_code AS VARCHAR) IN ({symbol_placeholders or "NULL"})
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
                WHERE (ktype IS NULL OR ktype = 'D')
                  AND CAST(stock_code AS VARCHAR) IN ({symbol_placeholders or "NULL"})
                ORDER BY stock_code, date
                """,
                prediction_symbols,
            )
        else:
            kline_rows = []
        dates = [str(row['trade_date'])[:10] for row in predictions + kline_rows]
        start = min(str(row['trade_date'])[:10] for row in predictions) if predictions else None
        calendar = open_session_dates(con, start, max(dates), strict=True) if start else []
    finally:
        con.close()

    prices, duplicates = {}, set()
    for row in kline_rows:
        key = (str(row['stock_code']), str(row['trade_date'])[:10])
        if key in prices:
            duplicates.add(key)
        prices[key] = row
    for key in duplicates:
        prices.pop(key)
    sessions = {day: index for index, day in enumerate(calendar)}
    evaluated_rows, excluded = [], []
    for prediction in predictions:
        day, code = str(prediction['trade_date'])[:10], str(prediction['symbol'])
        reason = None
        try:
            bars, execution_aware = _horizon_bars(prediction.get('horizon'))
            index = sessions[day]
            buy_day = calendar[index+1] if execution_aware else day
            sell_day = calendar[index+bars+int(execution_aware)]
            buy = prices[(code, buy_day)]
            sell = prices[(code, sell_day)]
            buy_price = float(buy['open' if execution_aware else 'close'])
            sell_price = float(sell['close'])
            score = float(prediction['score'])
            if not all(isfinite(x) for x in (buy_price, sell_price, score)) or min(buy_price, sell_price) <= 0:
                raise ValueError('invalid price or score')
        except (KeyError, IndexError):
            reason = 'exact_session_or_unique_bar_missing'
        except (ValueError, TypeError):
            reason = 'invalid_horizon_price_or_score'
        if reason:
            excluded.append({**prediction, 'reason': reason})
            continue
        forward_return = (sell_price / buy_price - 1.0) * 100
        evaluated_rows.append({**prediction, 'forward_return_pct': round(forward_return, 4),
            'net_forward_return_pct': round(forward_return - round_trip_cost_bps / 100.0, 4),
            'entry_date': buy_day, 'forward_date': sell_day,
            'label_mode': 'exact_session_open_to_close_diagnostic' if execution_aware else 'exact_session_close_to_close_diagnostic'})

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in evaluated_rows:
        grouped[row["model_id"]].append(row)

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
            ic = correlation(scores, day_returns)
            rank_ic = correlation(average_ranks(scores), average_ranks(day_returns))
            if ic is not None:
                daily_ics.append(ic)
            if rank_ic is not None:
                daily_rank_ics.append(rank_ic)
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
            "evaluation_method": "verified_session_cross_sectional_quantile_v2",
            "quantile": quantile,
            "round_trip_cost_bps": round_trip_cost_bps,
        }
        models[model_id] = item
    result = {"sample_count": len(evaluated_rows), "models": models, "rows": evaluated_rows,
              "excluded": excluded, "scope": "read_only_historical_shadow_diagnostic",
              "database_writes": 0, "execution_ready": False,
              "quantile": quantile, "round_trip_cost_bps": round_trip_cost_bps}
    return result
