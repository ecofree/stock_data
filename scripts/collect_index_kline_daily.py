"""Collect real daily index K-lines and publish the current index snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore, KPLClient
from collect_index import (
    collect_index_kline,
    collect_index_kline_eastmoney,
    sync_index_list_from_kline,
)
from config import DB_PATH, TODAY
from schema import init_schema

DEFAULT_INDEX_CODES = ["SH000001", "SZ399001", "SZ399006", "SH000688"]


def collect_index_kline_daily(db_path: str | Path, trade_date: str,
                              index_codes: list | None = None) -> dict:
    codes = index_codes or DEFAULT_INDEX_CODES
    con = duckdb.connect(str(db_path))
    try:
        init_schema(con)
    finally:
        con.close()
    store = DuckDBStore(db_path)
    client = KPLClient(request_timeout=30, max_attempts=2)
    fallback_rows = 0
    fallback_current = 0
    try:
        history_rows = collect_index_kline(client, store, trade_date, codes)
        current_rows = int(
            store.fetchall(
                "SELECT count(DISTINCT index_code) FROM index_kline "
                "WHERE CAST(date AS DATE)=CAST(? AS DATE) "
                "AND upper(coalesce(nullif(trim(ktype),''),'D'))='D'",
                [trade_date],
            )[0][0]
        )
        if current_rows < len(codes):
            existing_codes = {
                str(row[0])
                for row in store.fetchall(
                    "SELECT DISTINCT index_code FROM index_kline "
                    "WHERE CAST(date AS DATE)=CAST(? AS DATE) "
                    "AND upper(coalesce(nullif(trim(ktype),''),'D'))='D'",
                    [trade_date],
                )
            }
            missing_codes = [code for code in codes if code not in existing_codes]
            fallback_rows = collect_index_kline_eastmoney(store, trade_date, missing_codes)
            current_rows = int(
                store.fetchall(
                    "SELECT count(DISTINCT index_code) FROM index_kline "
                    "WHERE CAST(date AS DATE)=CAST(? AS DATE) "
                    "AND upper(coalesce(nullif(trim(ktype),''),'D'))='D'",
                    [trade_date],
                )[0][0]
            )
        fallback_current = int(
            store.fetchall(
                "SELECT count(DISTINCT index_code) FROM index_kline "
                "WHERE CAST(date AS DATE)=CAST(? AS DATE) "
                "AND upper(coalesce(nullif(trim(ktype),''),'D'))='D' "
                "AND coalesce(raw_json,'') LIKE '%eastmoney%'",
                [trade_date],
            )[0][0]
        )
        snapshot_rows = sync_index_list_from_kline(store, trade_date, codes)
    finally:
        store.close()
    status = (
        "fallback"
        if (fallback_rows or fallback_current) and current_rows >= min(3, len(codes)) and snapshot_rows == current_rows
        else "success"
        if current_rows >= min(3, len(codes)) and snapshot_rows == current_rows
        else "partial"
    )
    return {
        "trade_date": trade_date,
        "index_codes": len(codes),
        "history_rows": history_rows,
        "fallback_rows": fallback_rows,
        "fallback_current": fallback_current,
        "current_rows": current_rows,
        "snapshot_rows": snapshot_rows,
        "status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect index daily klines (full history).")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--codes", default=",".join(DEFAULT_INDEX_CODES))
    parser.add_argument("--out", default="reports/index_kline_collection_latest.md")
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    result = collect_index_kline_daily(args.db, args.date, codes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Index Kline Collection\n\n"
        f"- trade_date: `{result['trade_date']}`\n"
        f"- index codes: `{result['index_codes']}`\n"
        f"- history rows inserted/replaced: `{result['history_rows']}`\n"
        f"- fallback rows: `{result['fallback_rows']}`\n"
        f"- fallback current indexes: `{result['fallback_current']}`\n"
        f"- requested-date indexes: `{result['current_rows']}`\n"
        f"- current snapshot rows: `{result['snapshot_rows']}`\n"
        f"- status: `{result['status']}`\n",
        encoding="utf-8")
    print(" ".join(f"{k}={v}" for k, v in result.items()), f"report={out}")
    return 0 if result["status"] in {"success", "fallback"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
