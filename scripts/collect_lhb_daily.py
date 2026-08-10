"""采集指定交易日的龙虎榜全量数据（7 个 KPL 端点）。

龙虎榜在 2026-07 的 phase 化调度迁移中丢失了调用点（fetch_all.py 只 import
collect_all_lhb 而从未调用，且 ``--only-market`` 路径提前返回），导致 lhb_*
表自 07-08 停更。本脚本把 ``collect_all_lhb`` 重新接回 close phase，并可用于
按日回补历史缺口（KPL /lhb/* 端点接受 date 参数）。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_lhb import collect_all_lhb
from config import DB_PATH, TODAY
from schema import init_schema


def enrich_lhb_reason(db_path: str | Path, trade_date: str) -> int:
    """Fill missing KPL reasons from the direct Eastmoney data-center feed."""
    from trade_system.stock_data_sources import _from_em_dragon_tiger_daily

    try:
        payload = _from_em_dragon_tiger_daily(trade_date) or {}
    except Exception as exc:
        print(f"enrich_lhb_reason: eastmoney unavailable ({str(exc)[:200]})")
        return 0
    stocks = payload.get("stocks") or []
    if not stocks:
        return 0
    con = duckdb.connect(str(db_path))
    updated = 0
    try:
        for row in stocks:
            code = str(row.get("code") or "").strip()
            reason = str(row.get("reason") or "").strip()
            if len(code) == 6 and reason:
                con.execute(
                    "UPDATE lhb_list SET reason=? "
                    "WHERE CAST(date AS VARCHAR)=? AND stock_code=? "
                    "AND (reason IS NULL OR reason='')",
                    [reason, trade_date, code])
                updated += 1
        con.commit()
    finally:
        con.close()
    return updated


def collect_lhb_daily(db_path: str | Path, trade_date: str) -> dict:
    con = duckdb.connect(str(db_path))
    try:
        init_schema(con)
    finally:
        con.close()
    store = DuckDBStore(db_path)
    client = KPLClient(request_timeout=20, max_attempts=2)
    try:
        results = collect_all_lhb(client, store, trade_date)
    finally:
        store.close()
    total = sum(int(v or 0) for v in results.values())
    results["reason_enriched"] = enrich_lhb_reason(db_path, trade_date)
    failures = sum(
        int(client.stats.get(key) or 0)
        for key in ("error", "rate_limited", "circuit_open")
    )
    status = (
        "partial"
        if total and failures
        else "error"
        if failures
        else "success"
        if total
        else "empty"
    )
    return {
        "trade_date": trade_date,
        "total": total,
        "tables": results,
        "status": status,
        "provider_stats": dict(client.stats),
    }


def render_report(result: dict) -> str:
    lines = [
        "# LHB Daily Collection",
        "",
        f"- trade_date: `{result['trade_date']}`",
        f"- total rows: `{result['total']}`",
        f"- status: `{result['status']}`",
        f"- provider stats: `{result['provider_stats']}`",
        "",
        "| table | rows |",
        "| --- | --- |",
    ]
    for table, rows in result["tables"].items():
        lines.append(f"| {table} | {rows} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect the dragon-tiger board for one trade date.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--out", default="reports/lhb_collection_latest.md")
    args = parser.parse_args()
    result = collect_lhb_daily(args.db, args.date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    print(" ".join(f"{k}={v}" for k, v in result.items() if k != "tables"),
          f"tables={result['tables']}", f"report={out}")
    return 0 if result["status"] in {"success", "empty"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
