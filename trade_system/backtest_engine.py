"""Minimal strategy backtest engine for limit-up continuation strategies.

Scope (v1, deliberately simple and honest):
- Universe: stocks that closed limit-up on day D (from ``v_limit_pool``).
- Entry: next session's open, skipped when the open is an unfillable
  near-limit one-price board (一字板).
- T+1 is enforced structurally: the earliest exit session is entry+1.
- Exit: close of the session ``hold_days`` after entry.
- Costs: fixed slippage in bps applied to both sides.
- Sizing: equal targets from prior-close equity, debited from cash at entry.
- Daily cash/quantity ledger; missing marks are carried with explicit warnings.

This engine answers "would this rule have made money historically" at
first-order accuracy.  It does NOT model partial fills, intraday stops, or
20cm/ST boards differently — those are documented limitations, not secrets.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from statistics import median
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
    quantity: float = 0.0
    pnl: float = 0.0


def load_universe(con: duckdb.DuckDBPyConnection, start: str, end: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT CAST(trade_date AS DATE) AS d, stock_code, max(board_level) AS board
        FROM v_limit_pool
        WHERE CAST(trade_date AS DATE) BETWEEN ? AND ?
          AND board_level IS NOT NULL
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
    """Daily-bar research only; fractional units, slippage but no full fees.

    A missing close defers exit and carries the last observed mark. These
    valuations are flagged, not evidence of executable returns. Inputs must
    use one consistent price basis; corporate actions are not simulated here.
    """
    for name in ("hold_days", "max_positions", "board_min"):
        value = getattr(params, name)
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if params.board_max is not None and (
        type(params.board_max) is not int or params.board_max < params.board_min
    ):
        raise ValueError("board_max must be an integer >= board_min")
    for name in ("capital", "slippage_bps", "unfillable_open_ratio"):
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"{name} must be finite")
    if params.capital <= 0 or not 0 <= params.slippage_bps < 10000 or params.unfillable_open_ratio <= 1:
        raise ValueError("invalid capital, slippage or unfillable ratio")
    if sessions != sorted(set(sessions)):
        raise ValueError("sessions must be unique and increasing")

    def price(value):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return None
        return Decimal(str(value)) if isfinite(value) and value > 0 else None

    session_idx = {day: i for i, day in enumerate(sessions)}
    entries: dict[int, list[dict]] = {}
    for item in sorted(universe, key=lambda x: (x['date'], x['stock_code'])):
        si = session_idx.get(item['date'])
        if si is None or si + 1 >= len(sessions):
            continue
        if item['board'] < params.board_min or (
            params.board_max is not None and item['board'] > params.board_max
        ):
            continue
        entries.setdefault(si + 1, []).append(item)

    capital = Decimal(str(params.capital))
    cash = previous_equity = peak = capital
    slip = Decimal(str(params.slippage_bps)) / Decimal(10000)
    positions: dict[str, dict] = {}
    trades: list[Trade] = []
    ledger, intents = [], []
    curve = [float(capital)]
    max_dd = Decimal(0)
    skipped_unfillable = 0
    valuation_complete = True
    for i, day in enumerate(sessions):
        # Freeze this morning's target before any same-day entries or exits.
        target = previous_equity / params.max_positions
        seen = set()
        for item in entries.get(i, []):
            code = item['stock_code']
            if code in seen:
                continue
            seen.add(code)
            intent = {'signal_date': item['date'], 'entry_date': day, 'stock_code': code}
            intents.append(intent)
            bar = kline.get((day, code), {})
            prev = price(kline.get((item['date'], code), {}).get('close'))
            opening, high = price(bar.get('open')), price(bar.get('high'))
            if code in positions or len(positions) >= params.max_positions or cash <= 0:
                intent['status'] = 'blocked_capacity'
            elif prev is None or opening is None or high is None:
                intent['status'] = 'missing_entry_data'
            elif opening >= prev * Decimal(str(params.unfillable_open_ratio)) and opening >= high:
                # Retained legacy daily-bar fill approximation, not queue evidence.
                skipped_unfillable += 1
                intent['status'] = 'unfillable'
            else:
                entry_px = opening * (1 + slip)
                allocation = min(cash, target)
                quantity = allocation / entry_px
                cash -= allocation
                positions[code] = {
                    'stock_code': code, 'signal_date': item['date'], 'entry_date': day,
                    'exit_i': i + params.hold_days, 'entry_price': entry_px,
                    'quantity': quantity, 'cost': allocation, 'mark': opening,
                    'mark_date': day,
                }
                intent.update(status='filled', quantity=float(quantity), cost=float(allocation))

        stale = []
        for code, pos in list(positions.items()):
            close = price(kline.get((day, code), {}).get('close'))
            if close is None:
                stale.append(code)
                valuation_complete = False
                continue
            pos.update(mark=close, mark_date=day)
            if i >= pos['exit_i']:
                exit_px = close * (1 - slip)
                proceeds = pos['quantity'] * exit_px
                cash += proceeds
                trades.append(Trade(
                    stock_code=code, signal_date=pos['signal_date'], entry_date=pos['entry_date'],
                    exit_date=day, entry_price=round(float(pos['entry_price']), 4),
                    exit_price=round(float(exit_px), 4),
                    ret_pct=round(float((exit_px / pos['entry_price'] - 1) * 100), 4),
                    quantity=float(pos['quantity']), pnl=float(proceeds - pos['cost']),
                ))
                del positions[code]
        market_value = sum((p['quantity'] * p['mark'] for p in positions.values()), Decimal(0))
        equity = cash + market_value
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        ledger.append({'date': day, 'cash': float(cash), 'market_value': float(market_value),
                       'equity': float(equity), 'stale_marks': stale,
                       'positions': [{k: float(v) if isinstance(v, Decimal) else v for k, v in p.items()}
                                     for p in positions.values()]})
        curve.append(round(float(equity), 2))
        previous_equity = equity

    wins = [t for t in trades if t.ret_pct > 0]
    losses = [t for t in trades if t.ret_pct <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    return {
        "trades": trades,
        "equity_curve": curve,
        "daily_ledger": ledger,
        "intents": intents,
        "open_positions": ledger[-1]['positions'] if ledger else [],
        "contract_version": "daily_cash_quantity_v2",
        "scope": "research_only_daily_bar_proxy",
        "stats": {
            "n_trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "avg_ret_pct": round(sum(t.ret_pct for t in trades) / len(trades), 4)
            if trades else None,
            "median_ret_pct": round(median(t.ret_pct for t in trades), 4)
            if trades else None,
            "cumulative_return_pct": round(float((previous_equity / capital - 1) * 100), 3),
            "max_drawdown_pct": round(float(max_dd), 3),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
            "skipped_unfillable": skipped_unfillable,
            "open_positions": len(positions),
            "valuation_complete": valuation_complete,
        },
    }
