from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore
from config import DB_PATH, TODAY
from schema import init_schema
from trade_system.normalize import build_normalized_views
from trade_system.tushare_relay import (
    TushareRelayClient,
    TushareRelayError,
    collect_tushare_adj_factor,
    collect_tushare_daily,
    collect_tushare_daily_basic,
    collect_tushare_index_daily,
    collect_tushare_stock_basic,
    collect_tushare_trade_cal,
    sync_tushare_ohlc_to_core_tables,
)


SKIPPED: int | None = None


def _format_collection_line(table_name: str, result: int | None, total_rows: int) -> str:
    if result is SKIPPED:
        return f"{table_name}: skipped total_rows={total_rows}"
    return f"{table_name}: inserted_or_replaced={result} total_rows={total_rows}"


def _compact_date(value: str) -> str:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 8:
        return raw[:8]
    raise ValueError(f"invalid date: {value}")


def _default_start(end_date: str, days: int = 30) -> str:
    dt = datetime.strptime(_compact_date(end_date), "%Y%m%d")
    return (dt - timedelta(days=days)).strftime("%Y%m%d")


def _split_codes(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").replace(";", ",").split(",") if item.strip()]


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


def infer_stock_codes(db_path: str | Path, limit: int) -> list[str]:
    return _fetch_values(
        db_path,
        [
            (
                """
                SELECT stock_code
                FROM stock_candidate_stage_signal
                WHERE stock_code IS NOT NULL AND stock_code != ''
                GROUP BY stock_code
                ORDER BY max(score) DESC NULLS LAST, stock_code
                LIMIT ?
                """,
                [limit],
            ),
            (
                """
                SELECT stock_code
                FROM kline
                WHERE stock_code IS NOT NULL AND stock_code != ''
                GROUP BY stock_code
                ORDER BY count(*) DESC, stock_code
                LIMIT ?
                """,
                [limit],
            ),
            (
                """
                SELECT stock_code
                FROM tushare_stock_basic
                WHERE stock_code IS NOT NULL AND stock_code != ''
                ORDER BY stock_code
                LIMIT ?
                """,
                [limit],
            ),
        ],
        limit,
    )


def _count_relation(db_path: str | Path, relation: str) -> int:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return int(con.execute(f'SELECT count(*) FROM "{relation}"').fetchone()[0])
    except Exception:
        return 0
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect bounded TuShare relay basic data.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default=TODAY)
    parser.add_argument("--stock-codes", default="")
    parser.add_argument("--index-codes", default="SH000001,SZ399001,SZ399006")
    parser.add_argument("--max-stocks", type=int, default=5)
    parser.add_argument("--skip-stock-basic", action="store_true")
    parser.add_argument("--skip-trade-cal", action="store_true")
    parser.add_argument("--skip-daily", action="store_true")
    parser.add_argument("--skip-daily-basic", action="store_true")
    parser.add_argument("--skip-adj-factor", action="store_true")
    parser.add_argument("--skip-index-daily", action="store_true")
    parser.add_argument("--sync-core", action="store_true")
    args = parser.parse_args()

    end_date = _compact_date(args.end_date)
    start_date = _compact_date(args.start_date) if args.start_date else _default_start(end_date)

    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
    finally:
        store.close()

    stock_codes = _split_codes(args.stock_codes) or infer_stock_codes(args.db, max(1, args.max_stocks))
    stock_codes = stock_codes[: max(0, args.max_stocks)]
    index_codes = _split_codes(args.index_codes)

    client = TushareRelayClient()
    if not client.token:
        print("status=missing_token env=TUSHARE_FAST_RELAY_TOKEN|TUSHARE_RELAY_TOKEN|TUSHARE_TOKEN")
        return 2

    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
        collection_plan = [
            ("tushare_trade_cal", args.skip_trade_cal, lambda: collect_tushare_trade_cal(client, store, start_date, end_date)),
            ("tushare_stock_basic", args.skip_stock_basic, lambda: collect_tushare_stock_basic(client, store)),
            ("tushare_daily", args.skip_daily, lambda: collect_tushare_daily(client, store, stock_codes, start_date, end_date)),
            (
                "tushare_daily_basic",
                args.skip_daily_basic,
                lambda: collect_tushare_daily_basic(client, store, stock_codes, start_date, end_date),
            ),
            ("tushare_adj_factor", args.skip_adj_factor, lambda: collect_tushare_adj_factor(client, store, stock_codes, start_date, end_date)),
            (
                "tushare_index_daily",
                args.skip_index_daily,
                lambda: collect_tushare_index_daily(client, store, index_codes, start_date, end_date),
            ),
        ]
        results: dict[str, int | None] = {}
        for table_name, skipped, collect in collection_plan:
            if skipped:
                results[table_name] = SKIPPED
                print(f"{table_name}: skipped", flush=True)
                continue
            print(f"{table_name}: collecting", flush=True)
            results[table_name] = collect()
    except TushareRelayError as exc:
        print(f"status=relay_error detail={exc}")
        return 3
    finally:
        store.close()

    sync_result = {"kline": 0, "index_kline": 0}
    if args.sync_core:
        sync_result = sync_tushare_ohlc_to_core_tables(
            args.db,
            stock_codes=stock_codes,
            index_codes=index_codes,
            start_date=start_date,
            end_date=end_date,
        )
        build_normalized_views(args.db)

    print(f"status=ok start_date={start_date} end_date={end_date} stocks={len(stock_codes)} indexes={len(index_codes)}")
    for table_name, result in results.items():
        print(_format_collection_line(table_name, result, _count_relation(args.db, table_name)))
    if args.sync_core:
        print(f"kline_sync: inserted_or_replaced={sync_result['kline']} total_rows={_count_relation(args.db, 'kline')}")
        print(f"index_kline_sync: inserted_or_replaced={sync_result['index_kline']} total_rows={_count_relation(args.db, 'index_kline')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
