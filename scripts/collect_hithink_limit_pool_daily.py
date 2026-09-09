"""Persist the same-day HiThink official limit-up pool.

This is the close-stage producer for ``official_limit_pool``.  An empty or
obviously partial response never deletes the last verified snapshot; the
Eastmoney/KPL pool remains a separately marked fallback in ``v_limit_pool``.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.hithink_client import HiThinkClient, HiThinkError  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402
from trade_system.pipeline_runtime import PipelineLock  # noqa: E402
from trade_system.quality import table_exists  # noqa: E402


logger = get_logger("hithink_limit_pool_daily")
MIN_ROWS = 5


def _number(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _clean_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and deduplicate the provider response before any write."""
    cleaned: dict[str, dict[str, Any]] = {}
    for item in items:
        code = str(item.get("ticker") or item.get("stock_code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            continue
        cleaned[code] = {
            "ticker": code,
            "name": item.get("name") or item.get("stock_name") or "",
            "limit_up_time": item.get("limit_up_time") or item.get("last_seal_time") or "",
            "continue_day_cnt": _integer(item.get("continue_day_cnt") or item.get("limit_times")),
            "limit_up_reason": item.get("limit_up_reason") or item.get("reason") or "",
            "last_price": _number(item.get("last_price") or item.get("close")),
            "price_change_ratio_pct": _number(item.get("price_change_ratio_pct") or item.get("pct_chg")),
            "seal_money": _number(item.get("seal_money")),
            "max_seal_money": _number(item.get("max_seal_money")),
            "is_st": bool(item.get("is_st")),
        }
    return list(cleaned.values())


def _write_snapshot(con: duckdb.DuckDBPyConnection, trade_date: str,
                    items: list[dict[str, Any]]) -> int:
    if not table_exists(con, "official_limit_pool"):
        raise RuntimeError(
            "official_limit_pool is missing; apply schema migrations before the close run"
        )
    if len(items) < MIN_ROWS:
        raise RuntimeError(
            f"HiThink limit-up pool returned only {len(items)} valid rows; snapshot not replaced"
        )
    con.execute("BEGIN TRANSACTION")
    try:
        for item in items:
            con.execute(
                """
                INSERT INTO official_limit_pool
                    (trade_date, stock_code, stock_name, limit_up_time,
                     continue_day_cnt, limit_up_reason, close, pct_chg,
                     seal_money, max_seal_money, is_st, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hithink')
                ON CONFLICT (trade_date, stock_code) DO UPDATE SET
                    stock_name=excluded.stock_name,
                    limit_up_time=excluded.limit_up_time,
                    continue_day_cnt=excluded.continue_day_cnt,
                    limit_up_reason=excluded.limit_up_reason,
                    close=excluded.close,
                    pct_chg=excluded.pct_chg,
                    seal_money=excluded.seal_money,
                    max_seal_money=excluded.max_seal_money,
                    is_st=excluded.is_st,
                    source=excluded.source,
                    fetched_at=now()
                """,
                [
                    trade_date, item["ticker"], item["name"], item["limit_up_time"],
                    item["continue_day_cnt"], item["limit_up_reason"], item["last_price"],
                    item["price_change_ratio_pct"], item["seal_money"],
                    item["max_seal_money"], item["is_st"],
                ],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(items)


def collect(db_path: str | Path, trade_date: str) -> dict[str, Any]:
    datetime_date = date.fromisoformat(trade_date)
    client = HiThinkClient()
    items = _clean_items(client.limit_up_pool(datetime_date.isoformat()))
    con = duckdb.connect(str(db_path))
    try:
        count = _write_snapshot(con, trade_date, items)
    finally:
        con.close()
    return {
        "trade_date": trade_date,
        "source": "hithink",
        "status": "success",
        "rows": count,
        "calls": client.call_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    parser.add_argument("--date", required=True, help="same-day ISO trade date")
    parser.add_argument("--out", default=str(ROOT / "reports" / "hithink_limit_pool_latest.json"))
    parser.add_argument("--assume-pipeline-lock", action="store_true")
    args = parser.parse_args()
    configure()
    try:
        if args.assume_pipeline_lock:
            result = collect(args.db, args.date)
        else:
            with PipelineLock(args.db, f"hithink_limit_pool_{args.date}"):
                result = collect(args.db, args.date)
    except (HiThinkError, RuntimeError, ValueError) as exc:
        result = {"trade_date": args.date, "source": "hithink", "status": "failed", "error": str(exc)[:500]}
        logger.warning("HiThink official limit pool failed: %s", exc)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()), f"report={out}")
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
