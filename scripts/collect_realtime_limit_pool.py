"""采集同日真实涨停/连板池，供候选池和行动门槛使用。"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_l2 import collect_l2_realtime_all_boards
from collect_ladder import collect_ladder_realtime_boards
from config import DB_PATH, TODAY
from schema import init_schema
from trade_system.normalize import build_normalized_views
from trade_system.stock_data_sources import _from_em_zt_pool


def collect_realtime_limit_pool(db_path: str | Path, trade_date: str) -> dict:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    init_schema(con)
    con.close()
    source = "l2_realtime_all_boards"
    errors = []
    fetched_count = 0
    try:
        store = DuckDBStore(db_path)
        client = KPLClient(request_timeout=20, max_attempts=2)
        count = collect_l2_realtime_all_boards(client, store, trade_date)
        if not count:
            errors.append("KPL l2 realtime board returned 0")
            source = "ladder_realtime_boards"
            count = collect_ladder_realtime_boards(client, store, trade_date)
            if not count:
                errors.append("KPL ladder realtime board returned 0")
        fetched_count = int(count or 0)
        store.close()
    except Exception as exc:
        count = 0
        errors.append(str(exc)[:500])

    # The KPL credential can expire independently of its cached database rows.
    # Use Eastmoney's dedicated final limit-up pool as a bounded after-close
    # fallback; never relabel a morning KPL snapshot as a successful refresh.
    if fetched_count == 0:
        try:
            rows = _from_em_zt_pool("".join(ch for ch in trade_date if ch.isdigit())) or []
            if rows:
                from trade_system.db_utils import legacy_connect
                con = legacy_connect(str(db_path))
                try:
                    con.execute("BEGIN TRANSACTION")
                    try:
                        con.execute("DELETE FROM eastmoney_limit_up_pool WHERE date=?", [trade_date])
                        con.executemany(
                            "INSERT INTO eastmoney_limit_up_pool "
                            "(date,board_level,stock_code,stock_name,limit_up_time,fetched_at,raw_json) "
                            "VALUES (?,?,?,?,?,current_timestamp,?)",
                            [
                                [trade_date, int(row.get("limit_days") or 1), str(row.get("code") or ""),
                                 row.get("name") or "", row.get("last_seal") or row.get("first_seal") or "",
                                 json.dumps(row, ensure_ascii=False)]
                                for row in rows if str(row.get("code") or "").isdigit()
                            ],
                        )
                        con.execute("COMMIT")
                    except Exception:
                        try:
                            con.execute("ROLLBACK")
                        except Exception:
                            pass
                        raise
                finally:
                    con.close()
                fetched_count = len({str(row.get("code")) for row in rows if row.get("code")})
                source = "eastmoney_push2ex_zt_pool"
        except Exception as exc:
            errors.append(f"Eastmoney push2ex: {str(exc)[:400]}")

    build_normalized_views(db_path)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
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
            "VALUES (?,?,?,?,?,?,current_timestamp) ON CONFLICT(trade_date) DO UPDATE SET source=excluded.source,row_count=excluded.row_count,stock_count=excluded.stock_count,status=excluded.status,error=excluded.error,fetched_at=excluded.fetched_at",
            [trade_date, source, row_count, stock_count, status, error],
        )
        con.commit()
    finally:
        con.close()
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
        "Only same-date realtime board rows are promoted to `v_limit_pool`; THS snapshot rows are not substituted here.",
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
