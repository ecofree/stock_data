"""采集指定交易日的龙虎榜全量数据（7 个 KPL 端点）。

龙虎榜在 2026-07 的 phase 化调度迁移中丢失了调用点（fetch_all.py 只 import
collect_all_lhb 而从未调用，且 ``--only-market`` 路径提前返回），导致 lhb_*
表自 07-08 停更。本脚本把 ``collect_all_lhb`` 重新接回 close phase，并可用于
按日回补历史缺口（KPL /lhb/* 端点接受 date 参数）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_lhb import collect_all_lhb
from config import DB_PATH, TODAY
from schema import init_schema


def _collect_eastmoney_fallback(db_path: str | Path, trade_date: str) -> int:
    """Populate the summary table when the KPL LHB front door is unavailable.

    Eastmoney is a recovery source only.  The provider marker is retained in
    ``raw_json`` so the source can be audited instead of being mistaken for a
    KPL-authoritative batch.
    """
    from trade_system.stock_data_sources import _from_em_dragon_tiger_daily

    payload = _from_em_dragon_tiger_daily(trade_date) or {}
    stocks = payload.get("stocks") or []
    rows = []
    for item in stocks:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        rows.append(
            (
                trade_date,
                code,
                item.get("name") or "",
                str(item.get("change_pct") or ""),
                int(float(item.get("turnover") or 0)),
                item.get("reason") or "",
                int(float(item.get("buy_amount") or 0)),
                int(float(item.get("sell_amount") or 0)),
                int(float(item.get("net_amount") or 0)),
                json.dumps(
                    {"provider": "eastmoney_datacenter", "payload": item},
                    ensure_ascii=False,
                ),
            )
        )
    if not rows:
        return 0
    store = DuckDBStore(db_path)
    try:
        written = store.insert_rows(
            "lhb_list",
            rows,
            [
                "date", "stock_code", "stock_name", "change_pct", "turnover",
                "reason", "buy_amount", "sell_amount", "net_amount", "raw_json",
            ],
            replace_on=["date", "stock_code"],
        )
        store.log_collect("lhb_list", "eastmoney_datacenter", written, "fallback")
        return written
    finally:
        store.close()


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
    fallback_source = None
    fallback_error = None
    if not results.get("lhb_list"):
        try:
            fallback_rows = _collect_eastmoney_fallback(db_path, trade_date)
            if fallback_rows:
                results["lhb_list"] = fallback_rows
                total = sum(int(v or 0) for v in results.values())
                fallback_source = "eastmoney_datacenter"
        except Exception as exc:
            fallback_error = f"{type(exc).__name__}: {exc}"
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
        "fallback_source": fallback_source,
        "fallback_error": fallback_error,
    }


def render_report(result: dict) -> str:
    lines = [
        "# LHB Daily Collection",
        "",
        f"- trade_date: `{result['trade_date']}`",
        f"- total rows: `{result['total']}`",
        f"- status: `{result['status']}`",
        f"- provider stats: `{result['provider_stats']}`",
        f"- fallback source: `{result.get('fallback_source') or 'none'}`",
        f"- fallback error: `{result.get('fallback_error') or 'none'}`",
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
