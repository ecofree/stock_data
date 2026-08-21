from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_advanced_stock import (
    collect_advanced_dadan_kline,
    collect_advanced_main_activity_kline,
    collect_advanced_pankou,
    collect_advanced_zjmm_min,
)
from collect_l2 import (
    collect_l2_sector_intraday,
    collect_l2_sector_volume,
    collect_l2_stock_bigorder,
    collect_l2_stock_intraday,
)
from collect_sector import collect_sector_capital
from config import DB_PATH, TODAY
from schema import init_schema
from scripts.collect_intraday_capital_flow import infer_sector_codes, infer_stock_codes
from trade_system.capital_flow_health import (
    assess_capital_flow_health,
    render_capital_flow_health_markdown,
)
from trade_system.normalize import build_normalized_views
from trade_system.multi_source_store import MultiSourceStore
from trade_system import resilient_sources


def _parse_codes(value: str | None, limit: int) -> list[str]:
    if not value:
        return []
    codes = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    return codes[: max(0, limit)] if limit >= 0 else codes


def _current_flow_rows(data, trade_date: str, *, allow_undated_snapshot: bool = False) -> list[dict]:
    """Keep only rows that can be proven to belong to the requested session."""
    if not isinstance(data, list):
        return []
    rows = [row for row in data if isinstance(row, dict)]
    if not rows:
        return []
    dates = {
        "".join(ch for ch in str(row.get("date") or row.get("trade_date") or "") if ch.isdigit())[:8]
        for row in rows
        if row.get("date") or row.get("trade_date")
    }
    target = "".join(ch for ch in str(trade_date) if ch.isdigit())[:8]
    if dates and dates != {target}:
        return []
    if not dates and not allow_undated_snapshot:
        return []
    flow_fields = ("main_net", "super_net", "large_net", "mid_net", "small_net")
    return [row for row in rows if any(row.get(field) not in (None, "", "-") for field in flow_fields)]


def _collect_resilient_fallbacks(
    db_path: str,
    trade_date: str,
    stocks: list[str],
    sectors: list[str],
    *,
    started_monotonic: float | None = None,
    budget_seconds: float | None = None,
) -> dict[str, int]:
    """Probe the generic fallback graph only for gaps left by KPL.

    Historical fallback rows are deliberately rejected for an intraday date;
    only a live/refreshed snapshot or an explicitly dated response is stored.
    """
    counts = {"stock_rows": 0, "stock_codes": 0, "sector_rows": 0, "sector_codes": 0}

    def budget_available() -> bool:
        return not (
            started_monotonic is not None
            and budget_seconds is not None
            and time.monotonic() - started_monotonic >= max(0.0, float(budget_seconds))
        )

    if not budget_available():
        return counts
    with MultiSourceStore(db_path) as multi_store:
        existing_stock = {
            row[0] for row in multi_store.con.execute(
                "SELECT DISTINCT stock_code FROM multi_source_stock_flow WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE",
                [trade_date],
            ).fetchall()
        }
        for code in stocks:
            if not budget_available():
                break
            if code in existing_stock:
                continue
            try:
                data, meta = resilient_sources.get("stock_flow", code, ttl=0, periods=120)
            except Exception:
                continue
            if str(meta.get("status")) not in {"live", "refreshed"}:
                continue
            rows = _current_flow_rows(data, trade_date)
            if not rows:
                continue
            stored = multi_store.store("stock_flow", code, rows, meta, asset_type="stock", trade_date=trade_date)
            if stored.get("rows_written"):
                counts["stock_rows"] += int(stored["rows_written"])
                counts["stock_codes"] += 1

        existing_sector = multi_store.con.execute(
            "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE",
            [trade_date],
        ).fetchone()[0]
        if existing_sector < max(1, len(sectors)) and budget_available():
            try:
                data, meta = resilient_sources.get(
                    "sector_flow", None, ttl=0, date=trade_date, top_n=max(200, len(sectors))
                )
            except Exception:
                data, meta = None, {}
            if str(meta.get("status")) in {"live", "refreshed"}:
                rows = _current_flow_rows(data, trade_date, allow_undated_snapshot=True)
                if rows:
                    stored = multi_store.store("sector_flow", None, rows, meta, asset_type="sector", trade_date=trade_date)
                    counts["sector_rows"] = int(stored.get("rows_written") or 0)
                    counts["sector_codes"] = len({str(row.get("sector_code")) for row in rows if row.get("sector_code")})
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect bounded stock and sector capital-flow evidence.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--trade-date", "--date", dest="trade_date", default=TODAY)
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--max-sectors", type=int, default=20)
    parser.add_argument("--stock-codes", help="Comma-separated bounded stock-code override.")
    parser.add_argument("--sector-codes", help="Comma-separated bounded sector-code override.")
    parser.add_argument("--out", default="reports/capital_flow_freshness_latest.md")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--request-timeout", type=float, default=8.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--total-budget-seconds", type=float, default=45.0)
    parser.add_argument("--min-coverage-pct", type=float, default=80.0)
    parser.add_argument("--no-resilient-fallback", action="store_true", help="Do not probe the generic fallback graph after KPL gaps.")
    parser.add_argument(
        "--moneyflow-only",
        action="store_true",
        help="Collect only KPL advanced/zjmm-min for the bounded stock pool.",
    )
    args = parser.parse_args()

    if args.trade_date != date.today().isoformat():
        print(
            f"date={args.trade_date} status=historical_collection_blocked "
            "reason=focused endpoints include current-snapshot APIs"
        )
        return 2

    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
    finally:
        store.close()

    stocks = _parse_codes(args.stock_codes, args.max_stocks) or infer_stock_codes(
        args.db, args.trade_date, max(0, args.max_stocks)
    )
    sectors = _parse_codes(args.sector_codes, args.max_sectors) or infer_sector_codes(
        args.db, args.trade_date, max(0, args.max_sectors)
    )
    if not stocks or (not args.moneyflow_only and not sectors):
        print(
            f"date={args.trade_date} status=no_universe stocks={len(stocks)} sectors={len(sectors)}"
        )
        return 2 if args.strict else 0

    collection_started_at = datetime.now()
    collection_started_monotonic = time.monotonic()
    client = KPLClient(
        request_timeout=args.request_timeout,
        max_attempts=args.max_attempts,
        total_budget_seconds=args.total_budget_seconds,
    )
    store = DuckDBStore(args.db)
    try:
        if args.moneyflow_only:
            # One endpoint per candidate is the bounded independent flow
            # confirmation used by the close profile.  Do not fan out to the
            # other eight L2 endpoints merely to add a second money-flow
            # provider.
            results = {
                "advanced_zjmm_min": collect_advanced_zjmm_min(
                    client, store, args.trade_date, stocks
                )
            }
        else:
            results = {
                "sector_capital": collect_sector_capital(client, store, args.trade_date, sectors),
                "l2_sector_intraday": collect_l2_sector_intraday(client, store, args.trade_date, sectors),
                "l2_sector_volume": collect_l2_sector_volume(client, store, args.trade_date, sectors),
                "l2_stock_intraday": collect_l2_stock_intraday(client, store, args.trade_date, stocks),
                "l2_stock_bigorder": collect_l2_stock_bigorder(client, store, args.trade_date, stocks),
                "advanced_zjmm_min": collect_advanced_zjmm_min(client, store, args.trade_date, stocks),
                "advanced_dadan_kline": collect_advanced_dadan_kline(client, store, args.trade_date, stocks),
                "advanced_main_activity_kline": collect_advanced_main_activity_kline(
                    client, store, args.trade_date, stocks
                ),
                "advanced_pankou": collect_advanced_pankou(client, store, args.trade_date, stocks),
            }
    finally:
        store.close()

    # Keep the KPL intraday snapshot visible in the same source-aware layer as
    # Eastmoney/Sina, without summing cumulative minute points twice.
    with MultiSourceStore(args.db) as multi_store:
        kpl_source_rows = multi_store.sync_kpl_intraday_flow(args.trade_date)

    fallback_results = {"stock_rows": 0, "stock_codes": 0, "sector_rows": 0, "sector_codes": 0}
    if not args.no_resilient_fallback and not args.moneyflow_only:
        fallback_results = _collect_resilient_fallbacks(
            args.db,
            args.trade_date,
            stocks,
            sectors,
            started_monotonic=collection_started_monotonic,
            budget_seconds=args.total_budget_seconds,
        )

    build_normalized_views(args.db)
    health = assess_capital_flow_health(
        args.db,
        args.trade_date,
        len(stocks),
        len(sectors),
        collected_after=collection_started_at,
        min_coverage_pct=args.min_coverage_pct,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_capital_flow_health_markdown(health), encoding="utf-8")
    print(
        f"date={args.trade_date} stocks={len(stocks)} sectors={len(sectors)} "
        f"source_ready={str(health.get('source_ready', health['ready'])).lower()} "
        f"analysis_ready={str(health.get('analysis_ready', health.get('certified_ready', False))).lower()}"
    )
    for name in sorted(results):
        print(f"{name}={results[name]}")
    print(f"multi_source_kpl_stock_flow={kpl_source_rows}")
    print(f"resilient_fallback={fallback_results}")
    print(f"api_stats={client.stats} out={out}")
    return 0 if health["ready"] or not args.strict else 2


if __name__ == "__main__":
    raise SystemExit(main())
