"""Minimal strategy backtest engine for limit-up continuation strategies.

Scope (v1, deliberately simple and honest):
- Universe: stocks that closed limit-up on day D (from ``v_limit_pool``).
- Entry: next session's open, skipped when the open is an unfillable
  near-limit one-price board (一字板).
- T+1 is enforced structurally: the earliest exit session is entry+1.
- Exit: close of the session ``hold_days`` after entry.
- Costs: fixed slippage in bps applied to both sides.
- Sizing: equal fraction of current equity per slot, compounded sequentially.

This engine answers "would this rule have made money historically" at
first-order accuracy.  It does NOT model partial fills, intraday stops, or
20cm/ST boards differently — those are documented limitations, not secrets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

KLINE_DEDUP_CTE = """
SELECT CAST(trade_date AS DATE) AS d, stock_code, open, high, low, close FROM (
    SELECT CAST(trade_date AS DATE) AS trade_date, stock_code, open, high,
           low, close,
           row_number() OVER (
               PARTITION BY trade_date, stock_code
               ORDER BY is_fallback ASC, fetched_at DESC
           ) AS _rn
    FROM v_kline_daily WHERE ktype='D'
) WHERE _rn = 1
"""


@dataclass
class BacktestParams:
    hold_days: int = 1                 # exit at close of entry_session + hold_days
    slippage_bps: float = 10.0         # per side
    max_positions: int = 5             # equal slots; concurrent entries capped
    board_min: int = 1                 # universe filters on prior-day height
    board_max: int | None = None       # None = no upper bound
    unfillable_open_ratio: float = 1.095  # open >= prev_close * ratio => treat as 一字
    capital: float = 1_000_000.0


@dataclass
class Trade:
    stock_code: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    ret_pct: float


def load_universe(con: duckdb.DuckDBPyConnection, start: str, end: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT CAST(trade_date AS DATE) AS d, stock_code, max(board_level) AS board
        FROM v_limit_pool
        WHERE CAST(trade_date AS DATE) BETWEEN ? AND ?
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        [start, end],
    ).fetchall()
    return [{"date": str(r[0]), "stock_code": r[1], "board": int(r[2])} for r in rows]


def load_kline(con: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict]:
    rows = con.execute(
        f"SELECT d, stock_code, open, high, low, close FROM ({KLINE_DEDUP_CTE})"
    ).fetchall()
    out = {}
    for d, code, o, h, low, c in rows:
        out[(str(d), code)] = {"open": o, "high": h, "low": low, "close": c}
    return out


def simulate(
    universe: list[dict],
    kline: dict[tuple[str, str], dict],
    sessions: list[str],
    params: BacktestParams,
) -> dict[str, Any]:
    """Run the strategy; pure function over prepared inputs."""
    session_idx = {d: i for i, d in enumerate(sessions)}
    slip = params.slippage_bps / 10_000.0
    trades: list[Trade] = []
    skipped_unfillable = 0
    # One concurrent position per slot: a slot frees up when its trade exits.

    slot_free_at = [0] * params.max_positions  # earliest session index per slot

    for item in sorted(universe, key=lambda x: x["date"]):
        sig_d = item["date"]
        if params.board_max is not None and not (
            params.board_min <= item["board"] <= params.board_max
        ):
            continue
        if params.board_max is None and item["board"] < params.board_min:
            continue
        si = session_idx.get(sig_d)
        if si is None or si + 1 >= len(sessions):
            continue
        entry_i = si + 1                      # T+1: buy next morning at open
        exit_i = entry_i + params.hold_days   # sell at that session's close
        if exit_i >= len(sessions):
            continue
        prev_close = kline.get((sig_d, item["stock_code"]), {}).get("close")
        e_bar = kline.get((sessions[entry_i], item["stock_code"]))
        x_bar = kline.get((sessions[exit_i], item["stock_code"]))
        if not prev_close or not e_bar or not x_bar:
            continue
        if e_bar["open"] >= prev_close * params.unfillable_open_ratio \
                and e_bar["open"] >= e_bar["high"]:
            skipped_unfillable += 1
            continue

        free_slot = min(range(params.max_positions), key=lambda k: slot_free_at[k])
        if slot_free_at[free_slot] > entry_i:
            continue                          # all slots busy -> skip candidate
        slot_free_at[free_slot] = exit_i

        entry_px = e_bar["open"] * (1 + slip)
        exit_px = x_bar["close"] * (1 - slip)
        trades.append(Trade(
            stock_code=item["stock_code"],
            signal_date=sig_d,
            entry_date=sessions[entry_i],
            exit_date=sessions[exit_i],
            entry_price=round(entry_px, 4),
            exit_price=round(exit_px, 4),
            ret_pct=round((exit_px / entry_px - 1) * 100, 4),
        ))

    trades.sort(key=lambda t: t.exit_date)
    equity = params.capital
    curve: list[float] = [equity]
    frac = 1.0 / params.max_positions
    for t in trades:
        equity *= 1 + (t.ret_pct / 100.0) * frac
        curve.append(round(equity, 2))

    peak = params.capital
    max_dd_pct = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - value) / peak * 100)

    wins = [t for t in trades if t.ret_pct > 0]
    losses = [t for t in trades if t.ret_pct <= 0]
    gross_win = sum(t.ret_pct for t in wins)
    gross_loss = abs(sum(t.ret_pct for t in losses))
    return {
        "trades": trades,
        "equity_curve": curve,
        "stats": {
            "n_trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "avg_ret_pct": round(sum(t.ret_pct for t in trades) / len(trades), 4)
            if trades else None,
            "median_ret_pct": round(sorted(t.ret_pct for t in trades)[len(trades) // 2], 4)
            if trades else None,
            "cumulative_return_pct": round((curve[-1] / params.capital - 1) * 100, 3)
            if len(curve) > 1 else 0.0,
            "max_drawdown_pct": round(max_dd_pct, 3),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
            "skipped_unfillable": skipped_unfillable,
        },
    }
