"""Collect bounded, stock-level auction evidence during the auction window."""

from __future__ import annotations

import argparse
from datetime import datetime, time
import json
import os
from pathlib import Path
import sys

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base import DuckDBStore, KPLClient
from collect_misc import collect_auction_market
from config import API_KEY, TODAY
from schema import init_schema
from trade_system.api_health import require_api_key
from trade_system.stock_data_sources import _from_tencent_quote


def _ensure_batch(con: duckdb.DuckDBPyConnection) -> None:
    if os.environ.get("KPL_RUNTIME_SCHEMA_READY", "").strip() == "1":
        return
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS auction_collection_batch (
            trade_date DATE PRIMARY KEY,
            attempted_at TIMESTAMP,
            stock_codes INTEGER,
            tick_rows INTEGER,
            anomaly_rows INTEGER,
            quote_rows INTEGER DEFAULT 0,
            status VARCHAR,
            last_error VARCHAR
        )
        """
    )
    con.execute(
        "ALTER TABLE auction_collection_batch "
        "ADD COLUMN IF NOT EXISTS quote_rows INTEGER DEFAULT 0"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS auction_quote_snapshot (
            date DATE,
            stock_code VARCHAR,
            quote_time VARCHAR,
            indicative_price DOUBLE,
            cumulative_volume BIGINT,
            volume_unit VARCHAR,
            bid1_price DOUBLE,
            bid1_volume BIGINT,
            ask1_price DOUBLE,
            ask1_volume BIGINT,
            order_imbalance DOUBLE,
            provider VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        "ALTER TABLE auction_quote_snapshot ADD COLUMN IF NOT EXISTS volume_unit VARCHAR"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_auction_quote_snapshot "
        "ON auction_quote_snapshot(date,stock_code,quote_time,provider)"
    )


def _number(value):
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _quote_timestamp(value) -> datetime | None:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    for fmt, candidate in (
        ("%Y%m%d%H%M%S", digits[:14]),
        ("%Y%m%d%H%M", digits[:12]),
    ):
        if len(candidate) != len(datetime.now().strftime(fmt)):
            continue
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def _collect_tencent_auction_quotes(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    codes: list[str],
    *,
    quote_fetcher=_from_tencent_quote,
) -> int:
    """Persist a verified auction-window order-book snapshot.

    This is deliberately not written into ``auction_tick``: Tencent exposes a
    contemporaneous five-level quote snapshot, not exchange transaction
    ticks.  The distinct table and provider fields keep that semantic
    difference auditable while providing an independent KPL fallback.
    """
    payload = quote_fetcher(codes)
    if not isinstance(payload, dict):
        return 0
    written = 0
    for code, parts in payload.items():
        if not isinstance(parts, list) or len(parts) < 49:
            continue
        source_at = _quote_timestamp(parts[30] if len(parts) > 30 else "")
        if source_at is None or source_at.date().isoformat() != trade_date:
            continue
        if not time(9, 15) <= source_at.time() <= time(9, 27, 59):
            continue
        stock_code = str(parts[2] or code or "").strip()
        if len(stock_code) != 6 or not stock_code.isdigit():
            continue
        price = _number(parts[3])
        open_price = _number(parts[5])
        volume = _number(parts[6])
        bid1_price, bid1_volume = _number(parts[9]), _number(parts[10])
        ask1_price, ask1_volume = _number(parts[19]), _number(parts[20])
        indicative = (
            price if price and price > 0
            else open_price if open_price and open_price > 0
            else (bid1_price + ask1_price) / 2
            if bid1_price and ask1_price
            else bid1_price or ask1_price
        )
        if indicative is None:
            continue
        buy_volume = sum(_number(parts[index]) or 0 for index in (10, 12, 14, 16, 18))
        sell_volume = sum(_number(parts[index]) or 0 for index in (20, 22, 24, 26, 28))
        total_orders = buy_volume + sell_volume
        imbalance = (buy_volume - sell_volume) / total_orders if total_orders else None
        quote_time = source_at.strftime("%H:%M:%S")
        con.execute(
            "DELETE FROM auction_quote_snapshot "
            "WHERE date=? AND stock_code=? AND quote_time=? AND provider='tencent_qt'",
            [trade_date, stock_code, quote_time],
        )
        con.execute(
            """
            INSERT INTO auction_quote_snapshot
            (date,stock_code,quote_time,indicative_price,cumulative_volume,
             volume_unit,bid1_price,bid1_volume,ask1_price,ask1_volume,order_imbalance,
             provider,raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                trade_date, stock_code, quote_time, indicative,
                int(volume or 0), "hands", bid1_price, int(bid1_volume or 0),
                ask1_price, int(ask1_volume or 0), imbalance, "tencent_qt",
                json.dumps(
                    {
                        "source_timestamp": source_at.isoformat(),
                        "raw_fields": parts,
                        "semantic": "auction_order_book_snapshot_not_trade_tick",
                    },
                    ensure_ascii=False,
                ),
            ],
        )
        written += 1
    return written


def _codes(
    db_path: str,
    trade_date: str,
    limit: int,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[str]:
    """Resolve candidates using the caller's connection when one is open.

    DuckDB on Windows rejects opening a read-only connection to the same file
    while this process already owns a read-write connection.  The auction
    collector has exactly that lifecycle, so reusing ``con`` is required;
    standalone callers still get an owned read-only connection.
    """
    owns_connection = con is None
    connection = con if con is not None else duckdb.connect(db_path, read_only=True)
    try:
        for relation, date_col, order_sql in (
            ("v_limit_pool", "trade_date", "board_level DESC NULLS LAST, stock_code"),
            ("l2_realtime_all_boards", "date", "stock_code"),
            ("stock_candidate_stage_signal", "trade_date", "score DESC NULLS LAST, stock_code"),
        ):
            try:
                rows = connection.execute(
                    f"SELECT DISTINCT stock_code FROM {relation} WHERE CAST({date_col} AS VARCHAR)=? AND coalesce(stock_code,'')<>'' ORDER BY {order_sql} LIMIT ?",
                    [trade_date, int(limit)],
                ).fetchall()
            except Exception:
                continue
            if rows:
                return [str(row[0]) for row in rows]
        return []
    finally:
        if owns_connection:
            connection.close()


def collect(db_path: str, trade_date: str, *, max_stocks: int = 20, out: str | Path = "") -> dict:
    target = Path(out) if out else None
    result = {
        "trade_date": trade_date, "status": "outside_window", "stock_codes": 0,
        "tick_rows": 0, "anomaly_rows": 0, "quote_rows": 0,
    }
    def emit() -> dict:
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(" ".join(f"{key}={value}" for key, value in result.items()))
        return result
    now = datetime.now().time()
    in_window = time(8, 25) <= now <= time(9, 35)
    con = duckdb.connect(db_path)
    try:
        _ensure_batch(con)
        if not in_window:
            con.execute(
                """
                INSERT INTO auction_collection_batch
                (trade_date,attempted_at,stock_codes,tick_rows,anomaly_rows,quote_rows,status,last_error)
                VALUES (?,current_timestamp,0,0,0,0,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                  attempted_at=excluded.attempted_at,stock_codes=0,tick_rows=0,
                  anomaly_rows=0,quote_rows=0,status=excluded.status,last_error=excluded.last_error
                """,
                [trade_date, "outside_window", "auction collection is restricted to 08:25-09:35 local time"],
            )
            return emit()
        codes = _codes(db_path, trade_date, max_stocks, con=con)
        result["stock_codes"] = len(codes)
        if not codes:
            result["status"] = "no_candidates"
            con.execute(
                """
                INSERT INTO auction_collection_batch
                (trade_date,attempted_at,stock_codes,tick_rows,anomaly_rows,quote_rows,status,last_error)
                VALUES (?,current_timestamp,0,0,0,0,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                  attempted_at=excluded.attempted_at,stock_codes=0,tick_rows=0,
                  anomaly_rows=0,quote_rows=0,status=excluded.status,last_error=excluded.last_error
                """,
                [trade_date, "no_candidates", "same-date candidate pool is empty"],
            )
            return emit()
    finally:
        con.close()
    try:
        require_api_key(API_KEY)
        client = KPLClient()
        store = DuckDBStore(db_path)
        init_schema(store.conn)
        market = collect_auction_market(client, store, trade_date)
        tick_rows = int(market.get("tick_rows") or 0)
        quote_rows = int(market.get("quote_rows") or 0)
        # Anomaly collection moved out of the auction window: the old route
        # is a latest-session snapshot and is not a reliable same-date source.
        # The full-market route above is the authoritative auction input.
        anomaly_rows = 0
        if not tick_rows and not quote_rows:
            # Tencent remains a bounded order-book fallback for the rare case
            # where the full KPL route is temporarily empty.  It is written to
            # the separate quote table and never relabelled as trade ticks.
            quote_rows = _collect_tencent_auction_quotes(store.conn, trade_date, codes)
        store.close()
        result.update({
            "status": "success" if tick_rows or anomaly_rows or quote_rows else "empty",
            "tick_rows": tick_rows, "anomaly_rows": anomaly_rows,
            "quote_rows": quote_rows,
        })
        con = duckdb.connect(db_path)
        try:
            _ensure_batch(con)
            con.execute(
                """
                INSERT INTO auction_collection_batch
                (trade_date,attempted_at,stock_codes,tick_rows,anomaly_rows,quote_rows,status,last_error)
                VALUES (?,current_timestamp,?,?,?,?,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                  attempted_at=excluded.attempted_at,stock_codes=excluded.stock_codes,
                  tick_rows=excluded.tick_rows,anomaly_rows=excluded.anomaly_rows,
                  quote_rows=excluded.quote_rows,status=excluded.status,last_error=excluded.last_error
                """,
                [
                    trade_date, len(codes), tick_rows, anomaly_rows, quote_rows,
                    result["status"], "",
                ],
            )
        finally:
            con.close()
    except Exception as exc:
        result.update({"status": "error", "error": str(exc)})
        con = duckdb.connect(db_path)
        try:
            _ensure_batch(con)
            con.execute(
                """
                INSERT INTO auction_collection_batch
                (trade_date,attempted_at,stock_codes,tick_rows,anomaly_rows,quote_rows,status,last_error)
                VALUES (?,current_timestamp,?,0,0,0,'error',?)
                ON CONFLICT(trade_date) DO UPDATE SET
                  attempted_at=excluded.attempted_at,stock_codes=excluded.stock_codes,
                  tick_rows=0,anomaly_rows=0,quote_rows=0,status='error',
                  last_error=excluded.last_error
                """,
                [trade_date, len(codes), str(exc)[:1000]],
            )
        finally:
            con.close()
    return emit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect bounded auction tick/anomaly evidence.")
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--out", default=str(ROOT / "reports" / "auction_collection_latest.json"))
    args = parser.parse_args()
    result = collect(args.db, args.date, max_stocks=args.max_stocks, out=args.out)
    return 0 if result["status"] not in {"error"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
