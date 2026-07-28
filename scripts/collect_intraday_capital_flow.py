from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_advanced_stock import (
    collect_advanced_dadan_kline,
    collect_advanced_main_activity_kline,
    collect_advanced_main_monitor,
    collect_advanced_pankou,
    collect_advanced_zjmm_min,
)
from collect_l2 import (
    collect_l2_sector_intraday,
    collect_l2_sector_volume,
    collect_l2_tick_history,
    collect_l2_tick_orders,
    collect_l2_tick_orders_all,
)
from config import DB_PATH, TODAY
from schema import init_schema
from trade_system.normalize import build_normalized_views


def _fetch_values(db_path: str | Path, queries: list[tuple[str, list]], limit: int) -> list[str]:
    values: list[str] = []
    seen = set()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for sql, params in queries:
            if len(values) >= limit:
                break
            try:
                rows = con.execute(sql, params).fetchall()
            except Exception:
                continue
            for row in rows:
                value = str(row[0] or "").strip()
                if value and value not in seen:
                    seen.add(value)
                    values.append(value)
                if len(values) >= limit:
                    break
    finally:
        con.close()
    return values


def infer_stock_codes(db_path: str | Path, date: str, limit: int) -> list[str]:
    return _fetch_values(
        db_path,
        [
            (
                """
                SELECT stock_code
                FROM stock_candidate_stage_signal
                WHERE CAST(trade_date AS VARCHAR) = ? AND stock_code != ''
                GROUP BY stock_code
                ORDER BY max(score) DESC NULLS LAST, stock_code
                LIMIT ?
                """,
                [date, limit],
            ),
            (
                """
                SELECT stock_code
                FROM stock_candidate_score
                WHERE CAST(trade_date AS VARCHAR) = ? AND stock_code != ''
                GROUP BY stock_code
                ORDER BY max(score) DESC NULLS LAST, stock_code
                LIMIT ?
                """,
                [date, limit],
            ),
            (
                """
                SELECT stock_code
                FROM l2_realtime_all_boards
                WHERE CAST(date AS VARCHAR) = ? AND stock_code != ''
                GROUP BY stock_code
                ORDER BY max(board_level) DESC NULLS LAST, stock_code
                LIMIT ?
                """,
                [date, limit],
            ),
            (
                """
                SELECT stock_code
                FROM market_limit_up_down
                WHERE CAST(date AS VARCHAR) = ? AND stock_code != ''
                GROUP BY stock_code
                ORDER BY stock_code
                LIMIT ?
                """,
                [date, limit],
            ),
            (
                """
                SELECT stock_code
                FROM l2_tick_history
                WHERE CAST(date AS VARCHAR) = ? AND stock_code != ''
                GROUP BY stock_code
                ORDER BY count(*) DESC, stock_code
                LIMIT ?
                """,
                [date, limit],
            ),
        ],
        limit,
    )


def infer_sector_codes(db_path: str | Path, date: str, limit: int) -> list[str]:
    return _fetch_values(
        db_path,
        [
            (
                """
                SELECT sector_code
                FROM sector_capital
                WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' AND sector_code != '0'
                GROUP BY sector_code
                ORDER BY sector_code
                LIMIT ?
                """,
                [date, limit],
            ),
            (
                """
                SELECT sector_code
                FROM sector_ranking
                WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' AND sector_code != '0'
                GROUP BY sector_code
                ORDER BY sector_code
                LIMIT ?
                """,
                [date, limit],
            ),
            (
                """
                SELECT sector_code
                FROM sector_strength
                WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' AND sector_code != '0'
                GROUP BY sector_code
                ORDER BY max(strength_value) DESC NULLS LAST, sector_code
                LIMIT ?
                """,
                [date, limit],
            ),
            (
                """
                SELECT sector_code
                FROM l2_sector_volume
                WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' AND sector_code != '0'
                GROUP BY sector_code
                ORDER BY count(*) DESC, sector_code
                LIMIT ?
                """,
                [date, limit],
            ),
        ],
        limit,
    )


def _count_today(db_path: str | Path, relation: str, date: str) -> int:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return int(
            con.execute(
                f'SELECT count(*) FROM "{relation}" WHERE CAST(date AS VARCHAR) = ?',
                [date],
            ).fetchone()[0]
        )
    except Exception:
        try:
            return int(
                con.execute(
                    f'SELECT count(*) FROM "{relation}" WHERE CAST(trade_date AS VARCHAR) = ?',
                    [date],
                ).fetchone()[0]
            )
        except Exception:
            return -1
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect bounded intraday capital-flow evidence.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--max-stocks", type=int, default=8)
    parser.add_argument("--max-sectors", type=int, default=6)
    args = parser.parse_args()

    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
    finally:
        store.close()

    stocks = infer_stock_codes(args.db, args.date, max(0, args.max_stocks))
    sectors = infer_sector_codes(args.db, args.date, max(0, args.max_sectors))
    if not stocks and not sectors:
        print(f"date={args.date} status=no_candidates stocks=0 sectors=0")
        build_normalized_views(args.db)
        return 0

    client = KPLClient()
    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
        results = {}
        if stocks:
            results["l2_tick_history"] = collect_l2_tick_history(client, store, args.date, stocks)
            results["l2_tick_orders"] = collect_l2_tick_orders(client, store, args.date, stocks)
            results["l2_tick_orders_all"] = collect_l2_tick_orders_all(client, store, args.date, stocks)
            results["advanced_pankou"] = collect_advanced_pankou(client, store, args.date, stocks)
            results["advanced_main_monitor"] = collect_advanced_main_monitor(client, store, args.date, stocks)
            results["advanced_zjmm_min"] = collect_advanced_zjmm_min(client, store, args.date, stocks)
            results["advanced_dadan_kline"] = collect_advanced_dadan_kline(client, store, args.date, stocks)
            results["advanced_main_activity_kline"] = collect_advanced_main_activity_kline(client, store, args.date, stocks)
        if sectors:
            results["l2_sector_intraday"] = collect_l2_sector_intraday(client, store, args.date, sectors)
            results["l2_sector_volume"] = collect_l2_sector_volume(client, store, args.date, sectors)
    finally:
        store.close()

    build_normalized_views(args.db)
    print(f"date={args.date} stocks={len(stocks)} sectors={len(sectors)}")
    for key in sorted(results):
        print(f"{key}: inserted_or_replaced={results[key]} today_rows={_count_today(args.db, key, args.date)}")
    for relation in ("v_intraday_capital_flow_evidence", "v_intraday_strength_evidence"):
        print(f"{relation}: today_rows={_count_today(args.db, relation, args.date)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
