"""Screen stocks with a trained QLib model's latest scores.

Reads ``qlib_prediction`` (written by predict_qlib_daily) and emits a
Top-N Chinese screening report.  Research-only output: it never enters the
operator plan without the manual outcome gate.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.logging_setup import configure  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--model-id", default="lgbm_screen_v1")
    parser.add_argument("--trade-date", default="",
                        help="默认取该模型最新预测日")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument(
        "--out", default=str(PROJECT_ROOT / "reports" / "qlib_screen_latest.md"))
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db, read_only=True)
    try:
        day = args.trade_date or con.execute(
            """SELECT max(trade_date) FROM qlib_prediction
               WHERE model_id=?""", [args.model_id]).fetchone()[0]
        if not day:
            print("该模型还没有任何预测记录")
            return 1
        rows = con.execute(
            f"""
            SELECT p.symbol, p.score, p."rank",
                   max(CASE WHEN o.trade_date IS NOT NULL THEN 1 ELSE 0 END) AS in_limit_pool
            FROM qlib_prediction p
            LEFT JOIN official_limit_pool o
              ON o.stock_code = regexp_replace(p.symbol, '^([A-Z]+)', '')
             AND CAST(o.trade_date AS VARCHAR) = ?
            WHERE p.model_id = ? AND p.trade_date = ?
            GROUP BY p.symbol, p.score, p."rank"
            ORDER BY p.score DESC LIMIT ?
            """,
            # Bind order follows the ? positions in the SQL:
            # o.trade_date, p.model_id, p.trade_date, LIMIT.
            [day, args.model_id, day, args.top],
        ).fetchall()
        model_row = con.execute(
            "SELECT status FROM qlib_model_registry WHERE model_id=?",
            [args.model_id],
        ).fetchone()
        status = model_row[0] if model_row else "unregistered"
    finally:
        con.close()

    lines = [
        f"# QLib 选股筛选 · {day}",
        "",
        f"- model: `{args.model_id}`（状态 {status}）",
        f"- 截面股票数：{len(rows)}（显示 Top {min(args.top, len(rows))}）",
        "- 状态说明：shadow = 仅研究参考，未通过人工结果门禁前不进入计划。",
        "",
        "| 排名 | 代码 | 模型分 | 连板池 |",
        "|---|---|---|---|",
    ]
    for symbol, score, rank, in_pool in rows:
        flag = "🔥 今日涨停池" if in_pool else ""
        lines.append(f"| {rank} | {symbol} | {score:.4f} | {flag} |")
    lines += [
        "",
        "> 模型分为多因子学习得分，非涨跌预测；仅供研究方向参考，不构成投资建议。",
        "",
    ]
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:12]))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
