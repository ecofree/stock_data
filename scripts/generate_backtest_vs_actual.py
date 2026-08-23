"""Compare backtest signals vs actual operator outcomes for a date range.

Shows: which candidates the model/strategy recommended, whether the
operator actually traded them, and the realized P&L — exposing gaps
between "model says buy" and "what actually happened".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.logging_setup import configure  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports" / "backtest_vs_actual_latest.md"))
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db, read_only=True)
    try:
        rows = con.execute("""
            SELECT s.stage, s.stock_code,
                   max(s.stock_name) AS stock_name,
                   count(*) AS signal_count,
                   max(o.execution_status) AS execution_status,
                   round(max(o.net_return_pct), 2) AS net_return_pct,
                   round(avg(s.score), 1) AS avg_score
            FROM stock_candidate_stage_signal s
            LEFT JOIN operator_trade_outcome o
              ON o.stock_code = s.stock_code
             AND o.trade_date = CAST(s.trade_date AS VARCHAR)
                 + INTERVAL 1 DAY
            WHERE CAST(s.trade_date AS DATE) BETWEEN ? AND ?
              AND s.signal_triggered = true
            GROUP BY s.stage, s.stock_code
            ORDER BY s.stage, signal_count DESC
        """, [args.start, args.end]).fetchall()
    finally:
        con.close()

    traded = [r for r in rows if r[4]]
    skipped = [r for r in rows if not r[4]]
    wins = [r for r in traded if r[5] and r[5] > 0]

    lines = [
        f"# 回测信号 vs 实盘执行 · {args.start} ~ {args.end}",
        "",
        f"- 模型触发信号：{len(rows)} 条（{len(set(r[0] for r in rows))} 个阶段）",
        f"- 实际交易：{len(traded)} 条",
        f"- 跳过/未操作：{len(skipped)} 条",
        f"- 已交易中盈利：{len(wins)} 笔（胜率 "
        f"{round(len(wins) / len(traded) * 100) if traded else '—'}%）",
        "",
        "| 阶段 | 代码 | 名称 | 信号数 | 执行状态 | 净收益% | 均分 |",
        "|---|---|---|---|---|---|---|",
    ]
    for stage, code, name, cnt, status, ret, score in rows[:30]:
        lines.append(
            f"| {stage} | {code} | {name or '—'} | {cnt} |"
            f" {status or 'skipped'} | {ret if ret is not None else '—'} | {score} |")
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
