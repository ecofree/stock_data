"""Weekly review: aggregate the last 5 sessions into one markdown report.

Sections: phase sequence, premium & promotion averages, top themes by
limit-up membership, candidate-pick archive, and operator stats.  All from
tables that already exist; no network calls.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.cycle import PHASE_CN  # noqa: E402
from trade_system.logging_setup import configure  # noqa: E402


def _sessions(con, end_date: str, n: int = 5) -> list[str]:
    rows = con.execute(
        """SELECT DISTINCT CAST(trade_date AS DATE) FROM official_limit_pool
           WHERE trade_date <= ? ORDER BY 1 DESC LIMIT ?""",
        [end_date, n],
    ).fetchall()
    return [str(r[0]) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--end-date", default=str(date.today()))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports" / "weekly_review_latest.md"))
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db, read_only=True)
    try:
        sessions = _sessions(con, args.end_date)
        if not sessions:
            print("no limit-pool sessions found")
            return 1
        start, end = sessions[-1], sessions[0]

        phases = con.execute(
            """SELECT CAST(trade_date AS VARCHAR), phase, score FROM market_cycle_phase
               WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date""",
            [start, end],
        ).fetchall()

        prem = con.execute(
            """SELECT round(avg(avg_pct), 2) AS avg_premium,
                      round(avg(win_rate) * 100, 1) AS avg_win_pct
               FROM limit_premium_matrix
               WHERE board_bucket='_all' AND prev_trade_date BETWEEN ? AND ?""",
            [start, end],
        ).fetchone()

        promo = con.execute(
            """SELECT from_board, round(avg(rate) * 100, 1) AS avg_rate, sum(candidates)
               FROM promotion_rate_matrix
               WHERE trade_date BETWEEN ? AND ? AND from_board <= 3
               GROUP BY 1 ORDER BY 1""",
            [start, end],
        ).fetchall()

        themes = con.execute(
            """
            SELECT h.concept_name, count(*) AS zt_cnt,
                   count(DISTINCT h.stock_code) AS stocks,
                   max(CAST(h.trade_date AS VARCHAR)) AS last_day
            FROM ths_concept_stock_history h
            WHERE h.date_verified AND CAST(h.trade_date AS DATE) BETWEEN ? AND ?
            GROUP BY 1 ORDER BY zt_cnt DESC LIMIT 12
            """,
            [start, end],
        ).fetchall() if con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='ths_concept_stock_history'"
        ).fetchone()[0] else []

        picks = con.execute(
            """SELECT rank, stock_code, stock_name, total_score FROM daily_stock_picks
               WHERE trade_date BETWEEN ? AND ? AND rank <= 5
               ORDER BY trade_date DESC, rank LIMIT 15""",
            [start, end],
        ).fetchall() if con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='daily_stock_picks'"
        ).fetchone()[0] else []

    finally:
        con.close()

    lines = [
        f"# 周度复盘 · {start} ~ {end}",
        "",
        "## 相位序列",
        "",
        " → ".join(f"{PHASE_CN.get(p, p)}({s})" for _, p, s in phases) or "—",
        "",
        "## 打板期望值（昨日涨停今日表现）",
        "",
        f"- 平均溢价：{prem[0] if prem else '—'}%　平均胜率：{prem[1] if prem else '—'}%",
        "",
        "## 晋级率（首板→3板）",
        "",
        "| 板位 | 平均晋级率 | 样本数 |",
        "|---|---|---|",
    ]
    for board, rate, n in promo:
        lines.append(f"| {board}板 | {rate}% | {n} |")
    lines += ["", "## 题材热度 Top12", "", "| 题材 | 涨停人次 | 个股数 | 最后活跃 |", "|---|---|---|---|"]
    for name, cnt, stocks, last in themes:
        lines.append(f"| {name} | {cnt} | {stocks} | {last} |")
    lines += ["", "## 本周候选池存档（每日 Top5）", ""]
    for rank, code, name, score in picks:
        lines.append(f"- {code} {name}（{score}分）")
    lines += ["", "> research-only，非投资建议。", ""]
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"weekly review: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
