"""Collect migrated multi-source market data into source-aware DuckDB tables.

Examples:
  python scripts/collect_multisource.py --stock-codes 000001,600519
  python scripts/collect_multisource.py --all --stock-codes 000001,600519
  python scripts/collect_multisource.py --offline --types stock_flow,sector_flow
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH, TODAY
from trade_system.multi_source_store import MultiSourceStore, infer_codes, new_run_id
from trade_system import resilient_sources


DEFAULT_TYPES = ("stock_flow", "sector_flow", "kline", "valuation")
ALL_TYPES = tuple(resilient_sources.SOURCE_PLAN)
GLOBAL_TYPES = {
    "stock_basic", "northbound", "hot_topics", "dragon_tiger_daily", "industry_rank",
    "zt_pool", "zb_pool", "dt_pool", "yzt_pool", "limit_up_sentiment", "news_cls",
    "news_em", "ths_limit_up", "ths_hot_list", "em_hot_rank", "ipo_calendar", "macro",
    "sector_flow", "northbound_hist",
}
INDEX_TYPES = {"index_kline", "index_spot"}


def _codes(value: str | None) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in (value or "").split(",") if x.strip()))


def _offline_fetch(data_type, code=None, **kwargs):
    if data_type == "kline":
        key = f"kline:{code}:{kwargs.get('fq', 'qfq')}"
    else:
        key = resilient_sources._cache_key(data_type, code, kwargs)
    value, ts = resilient_sources.cache.get(key)
    if value is None:
        return None, {"source": "cache", "status": "failed", "warning": "offline cache miss"}
    if data_type == "kline" and isinstance(value, list):
        start = "".join(ch for ch in str(kwargs.get("start", "19900101")) if ch.isdigit())[:8]
        end = "".join(ch for ch in str(kwargs.get("end", "20500101")) if ch.isdigit())[:8]
        value = [row for row in value if isinstance(row, dict) and start <= "".join(ch for ch in str(row.get("date", "")) if ch.isdigit())[:8] <= end]
    return value, {"source": "cache", "status": "fresh", "cached_at": ts, "warning": "offline mode"}


def _run_one(store, data_type, code, args):
    kwargs = {}
    if data_type in {"kline", "index_kline", "etf_kline", "cb_kline"}:
        kwargs.update(start=args.start, end=args.end, fq=args.fq)
    if data_type == "sector_flow":
        kwargs.update(top_n=args.max_sectors, date=args.date)
    if data_type in {"financials", "fund_flow", "fund_flow_120d", "stock_flow", "statements"}:
        kwargs["periods"] = args.periods
    if data_type in {"hot_topics", "dragon_tiger", "dragon_tiger_daily", "zt_pool", "zb_pool", "dt_pool",
                     "yzt_pool", "limit_up_sentiment", "ths_limit_up", "intraday"}:
        kwargs["date"] = args.date.replace("-", "") if data_type in {
            "zt_pool", "zb_pool", "dt_pool", "yzt_pool", "limit_up_sentiment", "ths_limit_up", "intraday"
        } else args.date
    if data_type == "stock_basic":
        kwargs["list_status"] = "L"
    if data_type == "northbound_hist":
        kwargs.update(start=args.start, end=args.end)
    if data_type in {"ipo_calendar"}:
        kwargs.update(start=args.start, end=args.end)
    if data_type == "macro":
        kwargs["indicators"] = ["gdp", "cpi", "ppi"]
    data, meta = store.fetch(data_type, code, **kwargs)
    asset_type = "index" if data_type in INDEX_TYPES else ("stock" if code else data_type)
    stored = store.store(data_type, code, data, meta, asset_type=asset_type, trade_date=args.date)
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
    parser.add_argument("--all", action="store_true", help="Run all migrated data types (bounded by code limits).")
    parser.add_argument("--offline", action="store_true", help="Use resilient cache only; never call the network.")
    parser.add_argument("--no-sync-core", action="store_true", help="Do not copy fresh sector flow into sector_capital.")
    parser.add_argument("--report", default="reports/multisource_collection_latest.md")
    args = parser.parse_args()

    types = tuple(x.strip() for x in (ALL_TYPES if args.all else (args.types.split(",") if args.types else DEFAULT_TYPES)) if x.strip())
    unknown = sorted(set(types) - set(ALL_TYPES))
    if unknown:
        parser.error(f"unknown data types: {', '.join(unknown)}")

    stock_codes = _codes(args.stock_codes)
    index_codes = _codes(args.index_codes)
    if not stock_codes and any(t not in GLOBAL_TYPES and t not in INDEX_TYPES for t in types):
        stock_codes = infer_codes(args.db, "multi_source_stock_flow", "stock_code", args.max_stocks)
    stock_codes = stock_codes[: max(0, args.max_stocks)]
    run_id = new_run_id()
    started = datetime.now()
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
                    data, meta, stored = _run_one(store, data_type, code, args)
                    rows = stored["rows_written"]
                    results.append({"data_type": data_type, "scope": code or "<market>",
                                    "status": stored["status"], "provider": stored["provider"],
                                    "rows": rows, "warning": str(meta.get("warning", "")).replace("|", "/")})
                except Exception as exc:
                    results.append({"data_type": data_type, "scope": code or "<market>", "status": "failed",
                                    "provider": "", "rows": 0, "warning": str(exc).replace("|", "/")[:160]})
        if "sector_flow" in types and not args.no_sync_core:
            store.sync_sector_capital(args.date)
        if not args.no_sync_core and ("kline" in types or INDEX_TYPES.intersection(types)):
            store.sync_core_klines()
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
    return 0 if not any(x["status"] == "failed" for x in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
