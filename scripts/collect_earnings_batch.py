"""Batch-fetch quarterly income statements for limit-pool stocks.

Extracts revenue_yoy and net_profit_yoy as earnings quality factors.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.hithink_client import HiThinkClient  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402

logger = get_logger("earnings_collector")

UPSERT = """
INSERT INTO earnings_calendar
    (trade_date, stock_code, report_period, revenue_yoy, net_profit_yoy, eps, roe)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (trade_date, stock_code) DO UPDATE SET
    report_period=excluded.report_period,
    revenue_yoy=excluded.revenue_yoy,
    net_profit_yoy=excluded.net_profit_yoy,
    eps=excluded.eps, fetched_at=now()
"""


def _to_ths(code: str) -> str:
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def _compute_yoy(items: list[dict]) -> tuple[float | None, float | None]:
    """Compare latest quarter with same quarter last year."""
    if len(items) < 2:
        return None, None
    latest = items[0]
    fy = latest.get("fiscal_year")
    fp = latest.get("fiscal_period")
    rev = latest.get("operating_income") or latest.get("revenue")
    profit = latest.get("net_profit") or latest.get("profit_total")

    rev_yoy = prof_yoy = None
    for prev in items[1:]:
        if (prev.get("fiscal_year") == (fy or 0) - 1
                and prev.get("fiscal_period") == fp):
            p_rev = prev.get("operating_income") or prev.get("revenue")
            p_profit = prev.get("net_profit") or prev.get("profit_total")
            if p_rev and rev:
                rev_yoy = round((rev / abs(p_rev) - 1) * 100, 1)
            if p_profit and profit:
                prof_yoy = round((profit / abs(p_profit) - 1) * 100, 1)
            break
    return rev_yoy, prof_yoy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--max-stocks", type=int, default=100)
    args = parser.parse_args()

    configure()
    client = HiThinkClient(min_interval=0.5)

    try:
        # Read-only probe: requesting the writer lock here would collide with
        # the close-phase pipeline for no benefit.
        con = duckdb.connect(args.db, read_only=True)
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT stock_code FROM official_limit_pool WHERE trade_date=?",
            [args.trade_date]).fetchall()]
        codes = codes[:args.max_stocks]
        con.close()
    except Exception as exc:
        print(f"DB error: {exc}")
        return 1

    print(f"fetching income statements for {len(codes)} stocks...")
    done = failed = 0

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    try:
        for i, code in enumerate(codes):
            ths = _to_ths(code)
            try:
                data = client._get(
                    "/api/a-share/financials/income-statements",
                    {"thscode": ths, "period": "quarterly", "limit": 8},
                )
                items = data.get("item") or []
                if not items:
                    continue

                it = items[0]
                period = f"Q{it.get('fiscal_period', '')}" if it.get("fiscal_period") else ""
                eps = it.get("basic_eps")
                rev_yoy, prof_yoy = _compute_yoy(items)

                con.execute(UPSERT, [
                    args.trade_date, code, period,
                    rev_yoy, prof_yoy, eps, None,
                ])
                done += 1
            except Exception as exc:
                logger.debug("%s income fetch failed: %s", code, exc)
                failed += 1

            if (i + 1) % 20 == 0:
                print(f"  progress: {i + 1}/{len(codes)}")
                time.sleep(0.5)

        n = con.execute(
            "SELECT count(*) FROM earnings_calendar WHERE trade_date=?",
            [args.trade_date]).fetchone()[0]
        print(f"done: fetched={done} failed={failed}; "
              f"earnings_calendar {n} rows for {args.trade_date}")
        print(client.quota_note)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
