"""收盘后采集竞价异动（KPL /auction/bidding-anomaly）。

该端点忽略请求中的 date 参数、只返回最近交易日的数据：盘中（08:25–09:35）
调用时返回的是上一交易日，被语义校验拒绝（auction_bidding_anomaly 自
2026-07-14 起停更的直接原因）。收盘后调用则返回当日数据，校验通过。
候选代码取自当日涨停池 ∪ 候选信号（上限 80，控制 API 预算）。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_misc import collect_auction_bidding_anomaly
from config import DB_PATH, TODAY
from schema import init_schema

MAX_CODES = 80


def _candidate_codes(db_path: str | Path, trade_date: str) -> list:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT DISTINCT stock_code FROM (
                SELECT stock_code FROM v_limit_pool WHERE CAST(trade_date AS VARCHAR)=?
                UNION
                SELECT stock_code FROM stock_candidate_stage_signal
                WHERE CAST(trade_date AS VARCHAR)=?
            ) LIMIT ?
            """,
            [trade_date, trade_date, MAX_CODES],
        ).fetchall()
        return [str(r[0]) for r in rows if r[0]]
    except Exception:
        return []
    finally:
        con.close()


def collect_auction_anomaly_daily(db_path: str | Path, trade_date: str) -> dict:
    codes = _candidate_codes(db_path, trade_date)
    if not codes:
        return {"trade_date": trade_date, "codes": 0, "anomaly_rows": 0,
                "status": "no_candidates"}
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        init_schema(con)
    finally:
        con.close()
    store = DuckDBStore(db_path)
    client = KPLClient(request_timeout=20, max_attempts=2)
    try:
        # The endpoint ignores both the code and date parameters and returns
        # the latest trading day's global anomaly list, so a single request
        # suffices; replace_on keeps repeated calls idempotent.
        rows = collect_auction_bidding_anomaly(client, store, trade_date, codes[:1])
    finally:
        store.close()
    failures = sum(
        int(client.stats.get(key) or 0)
        for key in ("error", "rate_limited", "circuit_open")
    )
    return {"trade_date": trade_date, "codes": len(codes), "anomaly_rows": rows,
            "status": "error" if failures and not rows else "partial" if failures else "success" if rows else "empty",
            "provider_stats": dict(client.stats)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect auction bidding anomalies after close.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--out", default="reports/auction_anomaly_collection_latest.md")
    args = parser.parse_args()
    result = collect_auction_anomaly_daily(args.db, args.date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Auction Anomaly Collection (after-close)\n\n"
        f"- trade_date: `{result['trade_date']}`\n"
        f"- candidate codes: `{result['codes']}`\n"
        f"- anomaly rows: `{result['anomaly_rows']}`\n"
        f"- status: `{result['status']}`\n",
        encoding="utf-8")
    print(" ".join(f"{k}={v}" for k, v in result.items()), f"report={out}")
    return 0 if result["status"] in {"success", "empty"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
