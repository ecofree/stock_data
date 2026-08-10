"""Bounded L2 stock-intraday/bigorder collection for candidate stocks."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.kpl_health import (  # noqa: E402
    boosted_l2_stock_limit,
    should_boost_alternative_sources,
)
from trade_system.l2_focus import collect_l2_focus  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect L2 stock curves for candidate universe (phase-mode safe)."
    )
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True)
    parser.add_argument("--max-stocks", type=int, default=40)
    parser.add_argument("--total-budget-seconds", type=float, default=90.0)
    parser.add_argument("--skip-bigorder", action="store_true")
    parser.add_argument(
        "--prefer-eastmoney-trends",
        action="store_true",
        help="Skip KPL probe and write Eastmoney trends2 minute curves directly.",
    )
    parser.add_argument(
        "--auto-boost-if-kpl-stale",
        action="store_true",
        help="Double max-stocks when kpl_stale_tracker is elevated.",
    )
    parser.add_argument(
        "--codes",
        default="",
        help="Optional comma-separated codes; default uses limit-pool/candidates.",
    )
    parser.add_argument("--out", default="", help="Optional markdown report path.")
    args = parser.parse_args()

    max_stocks = args.max_stocks
    boost_info = should_boost_alternative_sources(args.db, args.date)
    if args.auto_boost_if_kpl_stale and boost_info["boost"]:
        # A stale KPL source must not double the network workload during the
        # live window.  Keep the production request bounded; failed codes are
        # resumed by the checkpoint on the next run instead.
        max_stocks = min(60, boosted_l2_stock_limit(max_stocks, boost=True))
    # When KPL market context is already stale, prefer EM trends2 immediately
    # (KPL stock-intraday has been empty during the same sessions).
    prefer_em = bool(args.prefer_eastmoney_trends or boost_info.get("boost"))

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    result = collect_l2_focus(
        args.db,
        args.date,
        max_stocks=max_stocks,
        codes=codes,
        total_budget_seconds=args.total_budget_seconds,
        include_bigorder=not args.skip_bigorder,
        prefer_eastmoney_trends=prefer_em,
    )
    print(
        f"trade_date={result.get('trade_date')} status={result.get('status')} "
        f"source={result.get('source') or 'n/a'} "
        f"requested={result.get('requested_stocks')} codes_ok={result.get('stock_codes_ok')} "
        f"intraday_rows={result.get('intraday_rows')} bigorder_rows={result.get('bigorder_rows')} "
        f"kpl_boost={boost_info['boost']} stale={boost_info['consecutive_stale']}"
        + (f" error={result.get('error')}" if result.get("error") else "")
    )
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# L2 Focus Collection",
            "",
            f"- trade_date: `{result.get('trade_date')}`",
            f"- status: `{result.get('status')}`",
            f"- requested_stocks: `{result.get('requested_stocks')}`",
            f"- stock_codes_ok: `{result.get('stock_codes_ok')}`",
            f"- intraday_rows: `{result.get('intraday_rows')}`",
            f"- bigorder_rows: `{result.get('bigorder_rows')}`",
            f"- kpl_boost: `{boost_info['boost']}` ({boost_info['reason']})",
            f"- error: `{result.get('error') or ''}`",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"report={path}")

    status = str(result.get("status") or "")
    if status in {"success", "partial", "partial_circuit", "empty_universe"}:
        return 0 if status != "empty" else 2
    if status == "empty":
        return 2
    return 2 if status in {"error", "circuit_open"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
