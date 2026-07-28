"""Audit P2 reference-data coverage without making network requests."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


TABLES = (
    ("finance_summary", "财务摘要"),
    ("finance_income", "利润表"),
    ("finance_balance", "资产负债表"),
    ("finance_cashflow", "现金流量表"),
    ("finance_fetch_checkpoint", "财务采集检查点"),
    ("index_list", "指数列表"),
    ("index_kline", "指数日线"),
    ("etf_all", "ETF全量"),
    ("auction_tick", "竞价逐笔"),
    ("l2_stock_intraday", "个股L2分时"),
    ("l2_stock_bigorder", "个股L2大单"),
)


def _exists(con, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()[0])


def _count(con, table: str) -> int:
    return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def build_report(db: str, out: str) -> dict:
    con = duckdb.connect(db, read_only=True)
    lines = [
        "# P2 数据缺口审计",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 数据库：`{db}`",
        "",
        "| 表 | 含义 | 行数 | 状态 |",
        "|---|---|---:|---|",
    ]
    counts = {}
    for table, label in TABLES:
        if not _exists(con, table):
            count, status = 0, "缺表"
        else:
            count = _count(con, table)
            status = "有数据" if count else "缺失"
        counts[table] = count
        lines.append(f"| `{table}` | {label} | {count:,} | {status} |")
    checkpoint = {}
    if _exists(con, "finance_fetch_checkpoint"):
        for status, count in con.execute(
            "SELECT status, count(*) FROM finance_fetch_checkpoint GROUP BY status ORDER BY status"
        ).fetchall():
            checkpoint[str(status)] = int(count)
    lines.extend([
        "",
        "## 财务检查点",
        "",
        "```json",
        str(checkpoint),
        "```",
        "",
        "## 解释",
        "",
        "财务采集器按小批次、跨进程限速和检查点运行；`缺失`表示当前数据库尚未形成可用证据，不会用空值冒充成功。",
        "指数/ETF/竞价/L2仍需根据接口实际返回逐项补齐，审计脚本只读数据库，不会触发网站请求。",
    ])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    con.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--out", default="reports/p2_gap_audit_latest.md")
    args = parser.parse_args()
    counts = build_report(args.db, args.out)
    print(f"report={args.out} finance_summary={counts['finance_summary']} index_list={counts['index_list']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
