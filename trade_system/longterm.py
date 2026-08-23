"""Long-term review skeleton (monthly trend + valuation snapshot).

Placeholder module — the data collection pipeline already captures
daily kline and financial indicators. This page will grow as more
fundamental tracking is added.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402


def monthly_kline_summary(con, index_code: str = "SH000001",
                          months: int = 12) -> pd.DataFrame:
    rows = con.execute(
        """SELECT substr(CAST(trade_date AS VARCHAR),1,7) AS ym,
                  min(low) AS low, max(high) AS high,
                  arg_min(open, trade_date) AS open,
                  arg_max(close, trade_date) AS close
           FROM v_kline_daily
           WHERE stock_code = ? AND ktype='D'
           GROUP BY ym ORDER BY ym DESC LIMIT ?""",
        [index_code, months]).fetchall()
    return pd.DataFrame(rows, columns=["ym", "low", "high", "open", "close"])


def render_monthly(con) -> str:
    df = monthly_kline_summary(con)
    if df.empty:
        return "<p class='empty'>暂无指数数据</p>"
    lines = ["<table><tr><th>月份</th><th>开盘</th><th>收盘</th><th>最高</th><th>最低</th></tr>"]
    for _, r in df.iterrows():
        chg = round((r["close"] / r["open"] - 1) * 100, 2) if r["open"] else "—"
        color = "#ff5b6a" if chg > 0 else "#2ebd85"
        lines.append(
            f"<tr><td>{r['ym']}</td><td>{r['open']:.0f}</td>"
            f"<td style='color:{color}'>{r['close']:.0f} ({chg:+.1f}%)</td>"
            f"<td>{r['high']:.0f}</td><td>{r['low']:.0f}</td></tr>")
    lines.append("</table>")
    return "".join(lines)
