"""收盘后采集"上榜概率"预测（KPL /advanced/on-the-lhb，按 date 请求）。

该采集器历史上只在 fetch_all.py 的非 --only-market 路径可达（调度器永远带
--only-market），advanced_on_the_lhb 自 2026-07-08 停更。端点可能返回空
（上游预测产品时有时无），空返回是合法状态。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_advanced import collect_advanced_on_the_lhb
from config import DB_PATH, TODAY
from schema import init_schema


def collect_advanced_lhb_daily(db_path: str | Path, trade_date: str) -> dict:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        init_schema(con)
    finally:
        con.close()
    store = DuckDBStore(db_path)
    client = KPLClient(request_timeout=20, max_attempts=2)
    try:
        rows = collect_advanced_on_the_lhb(client, store, trade_date)
    finally:
        store.close()
    failures = sum(
        int(client.stats.get(key) or 0)
        for key in ("error", "rate_limited", "circuit_open")
    )
    return {"trade_date": trade_date, "rows": rows,
            "status": "error" if failures and not rows else "partial" if failures else "success" if rows else "empty",
            "provider_stats": dict(client.stats)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect on-the-lhb probability predictions.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--out", default="reports/advanced_lhb_collection_latest.md")
    args = parser.parse_args()
    result = collect_advanced_lhb_daily(args.db, args.date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Advanced On-The-LHB Collection\n\n"
        f"- trade_date: `{result['trade_date']}`\n"
        f"- rows: `{result['rows']}`\n"
        f"- status: `{result['status']}`\n",
        encoding="utf-8")
    print(" ".join(f"{k}={v}" for k, v in result.items()), f"report={out}")
    return 0 if result["status"] in {"success", "empty"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
