"""Collect migrated multi-source market data into source-aware DuckDB tables.

Examples:
  python scripts/collect_multisource.py --stock-codes 000001,600519
  python scripts/collect_multisource.py --all --stock-codes 000001,600519
  python scripts/collect_multisource.py --offline --types stock_flow,sector_flow
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.config import DB_PATH, TODAY
from trade_system.trading_calendar import previous_open_session
from trade_system.multi_source_store import MultiSourceStore, infer_codes, new_run_id
from trade_system import resilient_sources


DEFAULT_TYPES = ("stock_flow", "sector_flow", "kline", "valuation")
ALL_TYPES = tuple(t for t in resilient_sources.SOURCE_PLAN if t != "limit_up_sentiment")
GLOBAL_TYPES = {
    "stock_basic", "northbound", "hot_topics", "dragon_tiger_daily", "industry_rank",
    "zt_pool", "zb_pool", "dt_pool", "yzt_pool", "news_cls",
    "news_em", "ths_limit_up", "ths_hot_list", "em_hot_rank", "ipo_calendar", "macro",
    "sector_flow", "northbound_hist",
}
INDEX_TYPES = {"index_kline", "index_spot"}
CURRENT_ONLY_TYPES = {"stock_basic", "index_spot", "valuation", "sector_flow", "stock_flow",
                      "bid_ask", "intraday", "northbound"}


def _codes(value: str | None) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in (value or "").split(",") if x.strip()))


def _offline_fetch(data_type, code=None, **kwargs):
    return resilient_sources.get(data_type, code, offline=True, **kwargs)


def _run_one(store, data_type, code, args):
    kwargs = {}
    if data_type in {"kline", "index_kline", "etf_kline", "cb_kline"}:
        from trade_system.trading_calendar import open_session_dates
        kwargs.update(start=args.start, end=args.end, fq=args.fq,
                      expected_sessions=open_session_dates(store.con, args.start, args.end, strict=True))
    if data_type == "sector_flow":
        kwargs.update(top_n=args.max_sectors, date=args.date)
    if data_type in {"financials", "fund_flow", "fund_flow_120d", "stock_flow", "statements"}:
        kwargs["periods"] = args.periods
    if data_type in {"hot_topics", "dragon_tiger", "dragon_tiger_daily", "zt_pool", "zb_pool", "dt_pool",
                     "yzt_pool", "ths_limit_up", "intraday"}:
        kwargs["date"] = args.date.replace("-", "") if data_type in {
            "zt_pool", "zb_pool", "dt_pool", "yzt_pool", "ths_limit_up", "intraday"
        } else args.date
    if data_type == "stock_basic":
        kwargs["list_status"] = "L"
    if data_type in {"northbound_hist", *resilient_sources._EVENT_RANGES}:
        kwargs.update(start=args.start, end=args.end)
    if data_type in {"ipo_calendar"}:
        kwargs.update(start=args.start, end=args.end)
    if data_type == "macro":
        kwargs["indicators"] = ["gdp", "cpi", "ppi"]
    if data_type == "statements":
        kwargs["report_type"] = "lrb"
    if data_type in {"kline", "index_kline"}:
        kwargs["full_history"] = False
    if data_type in CURRENT_ONLY_TYPES:
        allowed = {date.today().isoformat(), previous_open_session(store.con, date.today().isoformat())}
        if args.date not in allowed:
            raise ValueError("current snapshot cannot be relabelled as historical")
    request_key = hashlib.sha256(json.dumps(
        [data_type, code, kwargs], sort_keys=True).encode()).hexdigest()
    checkpoint_params = [args.date, "explicit", request_key, data_type, code or ""]
    resume = getattr(args, "resume", False) and data_type not in CURRENT_ONLY_TYPES
    checkpoint = store.con.execute(
        "SELECT status,attempts,rows_written,provider,started_at,finished_at FROM multi_source_task_checkpoint "
        "WHERE trade_date=? AND stage=? AND task_name=? AND data_type=? AND asset_code=?",
        checkpoint_params).fetchone() if resume else None
    if checkpoint and checkpoint[0] == "success" and not getattr(args, "force", False):
        receipt = store.con.execute(
            "SELECT 1 FROM multi_source_observation WHERE data_type=? AND coalesce(asset_code,'')=? "
            "AND provider=? AND source_date=? AND is_stale=false "
            "AND observed_at BETWEEN ? AND ? AND status IN ('live','refreshed') LIMIT 1",
            [data_type, code or "", checkpoint[3], args.date, checkpoint[4], checkpoint[5]]).fetchone()
        if receipt:
            return None, {"status": "skipped"}, {"status": "skipped", "provider": checkpoint[3], "rows_written": 0}
    attempts = int(checkpoint[1] or 0) + 1 if checkpoint else 1
    if resume:
        store.con.execute(
            "INSERT INTO multi_source_task_checkpoint "
            "(trade_date,stage,task_name,data_type,asset_code,status,attempts,started_at,updated_at) "
            "VALUES (?,?,?,?,?,'running',?,current_timestamp,current_timestamp) "
            "ON CONFLICT(trade_date,stage,task_name,data_type,asset_code) DO UPDATE SET "
            "status='running',attempts=excluded.attempts,started_at=excluded.started_at,updated_at=excluded.updated_at",
            checkpoint_params + [attempts])
    if getattr(args, "force", False):
        kwargs["ttl"] = 0
    data, meta = store.fetch(data_type, code, **kwargs)
    asset_type = "index" if data_type in INDEX_TYPES else ("stock" if code else data_type)
    stored = store.store(data_type, code, data, meta, asset_type=asset_type, trade_date=args.date)
    observed_empty = data == [] and meta.get("coverage") == "complete_event_range"
    if (not data and not observed_empty) or meta.get("status") not in {"fresh", "live", "refreshed"}:
        stored["status"] = "stale" if meta.get("status") == "stale" else "failed"
    if resume:
        status = "success" if stored["status"] in {"fresh", "live", "refreshed"} else stored["status"]
        store.con.execute(
            "UPDATE multi_source_task_checkpoint SET status=?,rows_written=?,provider=?,finished_at=current_timestamp,"
            "updated_at=current_timestamp WHERE trade_date=? AND stage=? AND task_name=? AND data_type=? AND asset_code=?",
            [status, stored["rows_written"], stored["provider"], *checkpoint_params])
    return data, meta, stored


def _render_report(path: Path, args, run_id, results):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 多源迁移采集报告", "", f"- run_id: `{run_id}`",
        f"- requested_date: `{args.date}`", f"- start/end: `{args.start}` / `{args.end}`", "",
        "| data_type | code/scope | status | provider | rows_written | warning |",
        "|---|---|---|---:|---:|---|",
    ]
    for item in results:
        lines.append("| {data_type} | {scope} | {status} | {provider} | {rows} | {warning} |".format(**item))
    lines.extend(["", "## 解读", "", "- `fresh/live/refreshed` 表示本次结果可写入；`stale` 只记录降级事实，不覆盖新鲜行。",
                  "- 个股资金流写入 `multi_source_stock_flow`，板块资金流写入 `multi_source_sector_flow`。",
                  "- Tushare 仅作为明确的末级兜底；行情优先走 Baostock/pytdx/腾讯/新浪/东方财富。", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist migrated multi-source A-share data.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--start", default="20250101")
    parser.add_argument("--end", default=TODAY.replace("-", ""))
    parser.add_argument("--fq", default="qfq", choices=("qfq", "hfq", ""))
    parser.add_argument("--periods", type=int, default=120)
    parser.add_argument("--stock-codes")
    parser.add_argument("--index-codes", default="SH000001,SZ399001,SZ399006")
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--max-sectors", type=int, default=200)
    parser.add_argument("--types", help="Comma-separated data types; defaults to stock/sector flow, kline, valuation.")
    parser.add_argument("--resume", action="store_true", help="Reuse completed identical explicit requests with retained receipts.")
    parser.add_argument("--force", action="store_true", help="Explicitly refresh providers; bypass completed requests and cache.")
    parser.add_argument("--dry-run", action="store_true", help="List requests without opening a writer or calling providers.")
    parser.add_argument("--budget-seconds", type=float, default=300.0)
    parser.add_argument("--universe-table", choices=("stock_basic", "multi_source_stock_flow"), default="multi_source_stock_flow")
    parser.add_argument("--all", action="store_true", help="Run all migrated data types (bounded by code limits).")
    parser.add_argument("--offline", action="store_true", help="Use resilient cache only; never call the network.")
    parser.add_argument("--no-sync-core", action="store_true", help="Do not copy fresh sector flow into sector_capital.")
    parser.add_argument("--allow-core-sync", action="store_true", help="Explicitly allow this compatibility collector to promote rows into core tables.")
    parser.add_argument("--report", default="reports/multisource_collection_latest.md")
    args = parser.parse_args()

    types = tuple(x.strip() for x in (ALL_TYPES if args.all else (args.types.split(",") if args.types else DEFAULT_TYPES)) if x.strip())
    unknown = sorted(set(types) - set(ALL_TYPES))
    if unknown:
        parser.error(f"unknown data types: {', '.join(unknown)}")

    stock_codes = _codes(args.stock_codes)
    index_codes = _codes(args.index_codes)
    if not stock_codes and any(t not in GLOBAL_TYPES and t not in INDEX_TYPES for t in types):
        stock_codes = infer_codes(args.db, args.universe_table, "stock_code", args.max_stocks)
    stock_codes = stock_codes[: max(0, args.max_stocks)]
    if any(t not in GLOBAL_TYPES and t not in INDEX_TYPES for t in types) and not stock_codes:
        parser.error("no stock scope resolved; supply --stock-codes or a populated universe")
    if args.dry_run:
        print(json.dumps({"types": types, "stocks": stock_codes, "indexes": index_codes,
                          "start": args.start, "end": args.end}))
        return 0
    run_id = new_run_id()
    started = datetime.now()
    deadline = time.monotonic() + max(0.0, args.budget_seconds)
    results = []
    fetcher = _offline_fetch if args.offline else None

    with MultiSourceStore(args.db, fetcher=fetcher) as store:
        for data_type in types:
            codes = [None]
            if data_type in INDEX_TYPES:
                codes = index_codes
            elif data_type not in GLOBAL_TYPES:
                codes = stock_codes
            for code in codes:
                if code is None and data_type not in GLOBAL_TYPES:
                    continue
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("collection budget exhausted; pending request is not complete")
                    data, meta, stored = _run_one(store, data_type, code, args)
                    rows = stored["rows_written"]
                    results.append({"data_type": data_type, "scope": code or "<market>",
                                    "status": stored["status"], "provider": stored["provider"],
                                    "rows": rows, "warning": str(meta.get("warning", "")).replace("|", "/")})
                except Exception as exc:
                    results.append({"data_type": data_type, "scope": code or "<market>", "status": "failed",
                                    "provider": "", "rows": 0, "warning": str(exc).replace("|", "/")[:160]})
        # Compatibility collectors keep provider evidence separate. Core
        # promotion is opt-in; the integrated phase owns canonical writes.
        if "sector_flow" in types and args.allow_core_sync and not args.no_sync_core:
            store.sync_sector_capital(args.date)
        finished = datetime.now()
        for data_type in types:
            subset = [x for x in results if x["data_type"] == data_type]
            store.record_run(
                run_id=run_id, started=started, finished=finished, trade_date=args.date,
                data_type=data_type, asset_scope="market" if data_type in GLOBAL_TYPES else "stocks",
                requested=len(subset), success=sum(x["status"] in {"fresh", "live", "refreshed"} for x in subset),
                stale=sum(x["status"] == "stale" for x in subset), failed=sum(x["status"] == "failed" for x in subset),
                providers=sorted({x["provider"] for x in subset if x["provider"]}),
                errors=[x["warning"] for x in subset if x["status"] == "failed" and x["warning"]],
            )

    _render_report(Path(args.report), args, run_id, results)
    print(f"run_id={run_id} types={','.join(types)} results={len(results)} report={args.report}")
    return 0 if all(x["status"] in {"fresh", "live", "refreshed", "skipped"} for x in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
