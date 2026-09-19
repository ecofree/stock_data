"""采集同日真实涨停/连板池，供候选池和行动门槛使用。"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.data_store import DuckDBStore, KPLClient
from collectors.collect_l2 import collect_l2_realtime_all_boards
from collectors.collect_ladder import collect_ladder_realtime_boards
from trade_system.config import DB_PATH, TODAY
from trade_system.schema import init_schema
from trade_system.normalize import build_normalized_views
from trade_system.resilient_sources import get


def collect_realtime_limit_pool(db_path: str | Path, trade_date: str) -> dict:
    trade_date = date.fromisoformat(trade_date).isoformat()
    source = "l2_realtime_all_boards"
    errors = []
    fetched_count = 0
    received_at = None
    with closing(DuckDBStore(db_path)) as store:
        init_schema(store.conn)
        try:
            client = KPLClient(request_timeout=20, max_attempts=2)
            count = collect_l2_realtime_all_boards(client, store, trade_date)
            if not count:
                errors.append("KPL l2 realtime board returned 0")
                source = "ladder_realtime_boards"
                count = collect_ladder_realtime_boards(client, store, trade_date)
                if not count:
                    errors.append("KPL ladder realtime board returned 0")
            fetched_count = int(count or 0)
        except Exception as exc:
            errors.append(str(exc)[:500])

        if fetched_count == 0:
            try:
                rows, meta = get("zt_pool", date=trade_date)
                if (not rows or meta.get("source") != "eastmoney"
                        or meta.get("status") not in {"fresh", "live", "refreshed"}):
                    raise ValueError("Eastmoney limit-up receipt unavailable or expired")
                codes = [row.get("code") for row in rows]
                if (len(set(codes)) != len(codes)
                        or any(not isinstance(code, str) or len(code) != 6
                               or not code.isascii() or not code.isdigit() for code in codes)
                        or any(type(row.get("limit_days")) is not int or row["limit_days"] < 1 for row in rows)):
                    raise ValueError("limit-up pool identity or observed board height invalid")
                receipt_time = datetime.fromtimestamp(meta["received_at"])
                values = sorted([
                    (trade_date, row["limit_days"], row["code"], row.get("name") or "",
                     row.get("last_seal") or row.get("first_seal") or "", receipt_time,
                     json.dumps(row, ensure_ascii=False)) for row in rows
                ])
                # Reuse the receipt without aging it forward or rewriting an identical snapshot.
                with store.transaction() as con:
                    existing = con.execute(
                        "SELECT CAST(date AS VARCHAR),board_level,stock_code,stock_name,"
                        "limit_up_time,fetched_at,raw_json FROM eastmoney_limit_up_pool WHERE date=?",
                        [trade_date]).fetchall()
                    if sorted(existing) != values:
                        con.execute("DELETE FROM eastmoney_limit_up_pool WHERE date=?", [trade_date])
                        con.executemany("INSERT INTO eastmoney_limit_up_pool "
                                        "(date,board_level,stock_code,stock_name,limit_up_time,fetched_at,raw_json) "
                                        "VALUES (?,?,?,?,?,?,?)", values)
                received_at = receipt_time
                fetched_count = len(values)
                source = "eastmoney_push2ex_zt_pool"
            except Exception as exc:
                errors.append(f"Eastmoney push2ex: {str(exc)[:400]}")

    build_normalized_views(db_path)
    from trade_system.db_utils import legacy_connect
    with closing(legacy_connect(str(db_path))) as con:
        row_count, stock_count = con.execute(
            "SELECT count(*), count(DISTINCT stock_code) FROM v_limit_pool WHERE trade_date=?",
            [trade_date],
        ).fetchone()
        row_count = int(row_count or 0)
        stock_count = int(stock_count or 0)
        status = "success" if fetched_count > 0 else ("stale" if stock_count > 0 else "empty")
        error = "; ".join(errors)[:1000]
        con.execute(
            "INSERT INTO realtime_candidate_pool_snapshot(trade_date,source,row_count,stock_count,status,error,fetched_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET source=excluded.source,row_count=excluded.row_count,stock_count=excluded.stock_count,status=excluded.status,error=excluded.error,fetched_at=excluded.fetched_at",
            [trade_date, source, row_count, stock_count, status, error, received_at or datetime.now()],
        )
    return {"trade_date": trade_date, "source": source, "row_count": row_count,
            "stock_count": stock_count, "status": status, "error": error}


def render_report(result: dict) -> str:
    return "\n".join([
        "# Realtime Executable Candidate Pool",
        "",
        f"- trade_date: `{result['trade_date']}`",
        f"- source: `{result['source']}`",
        f"- status: `{result['status']}`",
        f"- rows: `{result['row_count']}`",
        f"- distinct stocks: `{result['stock_count']}`",
        f"- error: `{result.get('error') or ''}`",
        "",
        "Same-date board rows retain their source and receipt time; unavailable or expired receipts cannot refresh the pool.",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect same-day realtime limit-up board candidates.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--out", default="reports/realtime_candidate_pool_latest.md")
    args = parser.parse_args()
    if args.date != date.today().isoformat():
        print(f"date={args.date} status=historical_collection_blocked")
        return 2
    result = collect_realtime_limit_pool(args.db, args.date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()), f"report={out}")
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
