from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_advanced import collect_advanced_morning_bidding, collect_advanced_morning_bidding_list
from collect_index import collect_all_index
from collect_l2 import (
    collect_l2_realtime_index_list,
    collect_l2_realtime_index_trend,
    collect_l2_sector_volume,
    collect_l2_stock_bigorder,
    collect_l2_stock_intraday,
    collect_l2_tick_history,
    collect_l2_tick_orders,
    collect_l2_tick_orders_all,
)
from collect_misc import collect_auction_bidding_anomaly, collect_auction_tick, collect_kline
from collect_sector import (
    collect_sector_all_stocks,
    collect_sector_capital,
    collect_sector_son_plates,
    collect_sector_strength_batch,
    collect_sector_sub_concepts,
)
from config import API_KEY, TODAY
from schema import init_schema
from trade_system.api_health import require_api_key


def _distinct_values(store: DuckDBStore, sql: str, params=None, limit: int | None = None) -> list[str]:
    rows = store.fetchall(sql, params or [])
    values = [str(row[0]) for row in rows if row and row[0]]
    if limit is not None:
        values = values[:limit]
    return values


def _extend_unique(target: list[str], values: list[str], limit: int) -> None:
    seen = set(target)
    for value in values:
        if value and value not in seen:
            target.append(value)
            seen.add(value)
        if len(target) >= limit:
            return


def select_stock_codes_for_professional_collection(store: DuckDBStore, date: str, limit: int) -> list[str]:
    codes: list[str] = []
    priority_queries = [
        (
            """
            SELECT DISTINCT stock_code
            FROM stock_candidate_stage_signal
            WHERE CAST(trade_date AS VARCHAR) = ? AND stock_code != ''
            ORDER BY max(score) OVER (PARTITION BY stock_code) DESC NULLS LAST, stock_code
            """,
            [date],
        ),
        (
            """
            SELECT DISTINCT stock_code
            FROM v_limit_pool
            WHERE CAST(trade_date AS VARCHAR) = ? AND stock_code != ''
            ORDER BY stock_code
            """,
            [date],
        ),
        (
            """
            SELECT DISTINCT stock_code
            FROM v_operator_candidates
            WHERE CAST(trade_date AS VARCHAR) = ? AND stock_code != ''
            ORDER BY score DESC NULLS LAST, stock_code
            """,
            [date],
        ),
        (
            """
            SELECT DISTINCT stock_code
            FROM sector_stocks
            WHERE CAST(date AS VARCHAR) = ? AND stock_code != ''
            ORDER BY stock_code
            """,
            [date],
        ),
        (
            """
            SELECT DISTINCT stock_code
            FROM l2_realtime_all_boards
            WHERE CAST(date AS VARCHAR) = ? AND stock_code != ''
            ORDER BY stock_code
            """,
            [date],
        ),
    ]
    for sql, params in priority_queries:
        try:
            _extend_unique(codes, _distinct_values(store, sql, params, limit), limit)
        except Exception:
            continue
        if len(codes) >= limit:
            break
    return codes[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect professional critical data sources with bounded scope.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--max-stocks", type=int, default=50)
    parser.add_argument("--max-sectors", type=int, default=50)
    parser.add_argument("--skip-auction", action="store_true")
    parser.add_argument("--skip-sector-capital", action="store_true")
    parser.add_argument("--skip-kline", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-l2-stock", action="store_true")
    args = parser.parse_args()

    try:
        require_api_key(API_KEY)
    except RuntimeError as exc:
        print(f"Professional source collection aborted: {exc}")
        return 2

    client = KPLClient()
    store = DuckDBStore(args.db)
    init_schema(store.conn)

    stock_codes = select_stock_codes_for_professional_collection(store, args.date, args.max_stocks)
    sector_codes = _distinct_values(
        store,
        "SELECT DISTINCT sector_code FROM sector_ranking WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' ORDER BY sector_code",
        [args.date],
        args.max_sectors,
    )
    if not sector_codes:
        sector_codes = _distinct_values(
            store,
            "SELECT DISTINCT sector_code FROM sector_strength WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' ORDER BY sector_code",
            [args.date],
            args.max_sectors,
        )

    results = {}
    if not args.skip_auction:
        results["advanced_morning_bidding"] = collect_advanced_morning_bidding(client, store, args.date)
        results["advanced_morning_bidding_list"] = collect_advanced_morning_bidding_list(client, store, args.date)
        results["auction_tick"] = collect_auction_tick(client, store, args.date, stock_codes)
        results["auction_bidding_anomaly"] = collect_auction_bidding_anomaly(client, store, args.date, stock_codes)
    if not args.skip_sector_capital:
        results["sector_capital"] = collect_sector_capital(client, store, args.date, sector_codes)
        results["sector_all_stocks"] = collect_sector_all_stocks(client, store, args.date, sector_codes)
        results["sector_son_plates"] = collect_sector_son_plates(client, store, args.date, sector_codes)
        results["sector_sub_concepts"] = collect_sector_sub_concepts(client, store, args.date, sector_codes)
        results["sector_strength_batch"] = collect_sector_strength_batch(client, store, args.date, sector_codes)
    if not args.skip_kline:
        total = 0
        for stock_code in stock_codes:
            total += collect_kline(client, store, args.date, stock_code)
        results["kline"] = total
    if not args.skip_index:
        results.update(collect_all_index(client, store, args.date))
        results["l2_realtime_index_list"] = collect_l2_realtime_index_list(client, store, args.date)
        results["l2_realtime_index_trend"] = collect_l2_realtime_index_trend(client, store, args.date)
    if not args.skip_l2_stock:
        results["l2_stock_intraday"] = collect_l2_stock_intraday(client, store, args.date, stock_codes)
        results["l2_stock_bigorder"] = collect_l2_stock_bigorder(client, store, args.date, stock_codes)
        results["l2_tick_history"] = collect_l2_tick_history(client, store, args.date, stock_codes[:10])
        results["l2_tick_orders"] = collect_l2_tick_orders(client, store, args.date, stock_codes[:10])
        results["l2_tick_orders_all"] = collect_l2_tick_orders_all(client, store, args.date, stock_codes[:10])
        results["l2_sector_volume"] = collect_l2_sector_volume(client, store, args.date, sector_codes[:10])

    store.close()
    print("Professional source collection:")
    print(f"date={args.date} stocks={len(stock_codes)} sectors={len(sector_codes)}")
    for name, rows in sorted(results.items()):
        print(f"{name}={rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
