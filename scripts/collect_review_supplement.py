"""Run the bounded P1 after-close review supplement.

The production close path intentionally does not call the legacy ``fetch_all``
fan-out.  This command is the explicit, resumable lane for review-enhancement
tables that have a usable KPL endpoint but are not required for close readiness.
It uses one DuckDB writer and a bounded sector universe; it never relabels a
historical snapshot because several KPL routes expose the current snapshot
without a reliable historical-date parameter.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))

import duckdb

from base import DuckDBStore, KPLClient
from schema import init_schema
from trade_system.pipeline_runtime import PipelineLock
from trade_system.trading_calendar import latest_open_session

from collect_advanced import (
    collect_advanced_his_ranking,
    collect_advanced_his_sharp_withdrawal,
    collect_advanced_his_zhangfu_detail,
    collect_advanced_newhigh_group,
    collect_advanced_weight_performance,
    collect_advanced_weipan_qiangchou,
    collect_advanced_zhangting_expression,
)
from collect_dingpan import collect_dingpan_jijin
from collect_sector import (
    collect_sector_bk_fenshi_zhibo,
    collect_sector_son_plates,
    collect_sector_sub_concepts,
)


P1_TABLES = (
    "advanced_his_ranking",
    "advanced_his_sharp_withdrawal",
    "advanced_his_zhangfu_detail",
    "advanced_newhigh_group_count",
    "advanced_newhigh_group_stocks",
    "advanced_weight_performance",
    "advanced_weipan_qiangchou",
    "advanced_zhangting_expression",
    "dingpan_jijin",
    "sector_bk_fenshi_zhibo",
    "sector_son_plates",
    "sector_sub_concepts",
)


def _sector_codes(con: duckdb.DuckDBPyConnection, limit: int) -> list[str]:
    for query, params in (
        ("SELECT sector_code FROM sector_plates WHERE sector_code IS NOT NULL ORDER BY sector_code LIMIT ?", [limit]),
        ("SELECT DISTINCT sector_code FROM sector_ranking WHERE sector_code IS NOT NULL ORDER BY sector_code LIMIT ?", [limit]),
    ):
        try:
            rows = con.execute(query, params).fetchall()
        except Exception:
            continue
        codes = [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]
        if codes:
            return codes
    return []


def _ensure_batch_table(con: duckdb.DuckDBPyConnection) -> None:
    if os.environ.get("KPL_RUNTIME_SCHEMA_READY", "").strip() == "1":
        return
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS review_supplement_batch (
            trade_date DATE PRIMARY KEY,
            attempted_at TIMESTAMP,
            status VARCHAR,
            rows_written INTEGER,
            api_success INTEGER,
            api_error INTEGER
        )
        """
    )


def _selected_tables(raw: str) -> list[str]:
    requested = [item.strip() for item in raw.split(",") if item.strip()]
    if not requested or requested == ["p1"]:
        return list(P1_TABLES)
    unknown = sorted(set(requested) - set(P1_TABLES))
    if unknown:
        raise ValueError(f"unsupported P1 table(s): {', '.join(unknown)}")
    return requested


def collect_review_supplement(
    db_path: str | Path,
    trade_date: str,
    *,
    tables: str = "p1",
    max_sectors: int = 20,
    dry_run: bool = False,
    out: str | Path | None = None,
) -> dict:
    selected = _selected_tables(tables)
    latest = latest_open_session(db_path)
    if latest and str(trade_date) != str(latest):
        raise ValueError(
            f"P1 supplement is current-snapshot only: requested {trade_date}, latest verified session is {latest}"
        )

    sector_codes: list[str] = []
    if not dry_run:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            sector_codes = _sector_codes(con, max(1, int(max_sectors)))
        finally:
            con.close()
    calls = {
        "advanced_his_ranking": "KPL /advanced/his-ranking?date=...",
        "advanced_his_sharp_withdrawal": "KPL /advanced/his-sharp-withdrawal?date=...",
        "advanced_his_zhangfu_detail": "KPL /advanced/his-zhangfu-detail (raw grouped distribution)",
        "advanced_newhigh_group_count": "KPL /advanced/newhigh-group-count + group-stocks",
        "advanced_newhigh_group_stocks": "KPL /advanced/newhigh-group-count + group-stocks",
        "advanced_weight_performance": "KPL /advanced/weight-performance?date=...",
        "advanced_weipan_qiangchou": "KPL /advanced/weipan-qiangchou",
        "advanced_zhangting_expression": "KPL /advanced/zhangting-expression?date=...",
        "dingpan_jijin": "KPL /dingpan/jijin",
        "sector_bk_fenshi_zhibo": f"KPL /sector/bk-fenshi-zhibo (up to {len(sector_codes) or max_sectors} sectors)",
        "sector_son_plates": f"KPL /sector/son-plates (up to {len(sector_codes) or max_sectors} sectors)",
        "sector_sub_concepts": f"KPL /sector/sub-concepts (up to {len(sector_codes) or max_sectors} sectors)",
    }
    result = {
        "trade_date": trade_date,
        "latest_verified_session": latest,
        "selected_tables": selected,
        "max_sectors": int(max_sectors),
        "sector_codes": len(sector_codes),
        "dry_run": bool(dry_run),
        "status": "planned" if dry_run else "running",
        "rows": {},
        "calls": {name: calls[name] for name in selected},
    }
    if dry_run:
        return result

    store = DuckDBStore(str(db_path))
    client = KPLClient(max_attempts=2, total_budget_seconds=120)
    try:
        init_schema(store.conn)
        _ensure_batch_table(store.conn)
        if "advanced_his_ranking" in selected:
            result["rows"]["advanced_his_ranking"] = collect_advanced_his_ranking(client, store, trade_date)
        if "advanced_his_sharp_withdrawal" in selected:
            result["rows"]["advanced_his_sharp_withdrawal"] = collect_advanced_his_sharp_withdrawal(client, store, trade_date)
        if "advanced_his_zhangfu_detail" in selected:
            result["rows"]["advanced_his_zhangfu_detail"] = collect_advanced_his_zhangfu_detail(client, store, trade_date)
        if {"advanced_newhigh_group_count", "advanced_newhigh_group_stocks"} & set(selected):
            value = collect_advanced_newhigh_group(client, store, trade_date)
            for name in ("advanced_newhigh_group_count", "advanced_newhigh_group_stocks"):
                if name in selected:
                    result["rows"][name] = value
        if "advanced_weight_performance" in selected:
            result["rows"]["advanced_weight_performance"] = collect_advanced_weight_performance(client, store, trade_date)
        if "advanced_weipan_qiangchou" in selected:
            result["rows"]["advanced_weipan_qiangchou"] = collect_advanced_weipan_qiangchou(client, store, trade_date)
        if "advanced_zhangting_expression" in selected:
            result["rows"]["advanced_zhangting_expression"] = collect_advanced_zhangting_expression(client, store, trade_date)
        if "dingpan_jijin" in selected:
            result["rows"]["dingpan_jijin"] = collect_dingpan_jijin(client, store, trade_date)
        if sector_codes:
            if "sector_bk_fenshi_zhibo" in selected:
                result["rows"]["sector_bk_fenshi_zhibo"] = collect_sector_bk_fenshi_zhibo(client, store, trade_date, sector_codes)
            if "sector_son_plates" in selected:
                result["rows"]["sector_son_plates"] = collect_sector_son_plates(client, store, trade_date, sector_codes)
            if "sector_sub_concepts" in selected:
                result["rows"]["sector_sub_concepts"] = collect_sector_sub_concepts(client, store, trade_date, sector_codes)
        else:
            for name in ("sector_bk_fenshi_zhibo", "sector_son_plates", "sector_sub_concepts"):
                if name in selected:
                    result["rows"][name] = 0
                    result.setdefault("deferred", []).append(f"{name}:sector_universe_missing")
        result["status"] = "completed" if any(int(value or 0) > 0 for value in result["rows"].values()) else "empty"
        result["table_rows"] = {}
        for table_name in selected:
            try:
                columns = {
                    row[1]
                    for row in store.conn.execute(
                        "PRAGMA table_info('" + table_name + "')"
                    ).fetchall()
                }
                if "date" in columns:
                    count_query = f'SELECT count(*) FROM "{table_name}" WHERE CAST(date AS VARCHAR)=?'
                    params = [trade_date]
                else:
                    count_query = f'SELECT count(*) FROM "{table_name}"'
                    params = []
                result["table_rows"][table_name] = int(
                    store.conn.execute(count_query, params).fetchone()[0]
                )
            except Exception:
                result["table_rows"][table_name] = None
        # Report the verified same-date/current relation counts.  Collector
        # functions may cover more than one table (notably new-high groups),
        # so their aggregate return value is not a per-table row count.
        for table_name, count in result["table_rows"].items():
            if count is not None:
                result["rows"][table_name] = count
        result["api_success"] = client.stats.get("success", 0)
        result["api_error"] = client.stats.get("error", 0)
        total_rows = sum(int(value or 0) for value in result["rows"].values())
        store.conn.execute(
            """
            INSERT INTO review_supplement_batch
            (trade_date,attempted_at,status,rows_written,api_success,api_error)
            VALUES (CAST(? AS DATE),current_timestamp,?,?,?,?)
            ON CONFLICT(trade_date) DO UPDATE SET
                attempted_at=excluded.attempted_at,
                status=excluded.status,
                rows_written=excluded.rows_written,
                api_success=excluded.api_success,
                api_error=excluded.api_error
            """,
            [trade_date, result["status"], total_rows, int(result["api_success"]), int(result["api_error"])],
        )
        return result
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    parser.add_argument("--date", required=True, help="latest verified open session, YYYY-MM-DD")
    parser.add_argument("--tables", default="p1", help="p1 or comma-separated P1 table names")
    parser.add_argument("--max-sectors", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--assume-pipeline-lock",
        action="store_true",
        help="The caller already owns the project pipeline lock (used by the close runner).",
    )
    parser.add_argument("--out", default=str(ROOT / "reports" / "review_supplement_latest.json"))
    args = parser.parse_args()
    try:
        if args.dry_run:
            result = collect_review_supplement(args.db, args.date, tables=args.tables, max_sectors=args.max_sectors, dry_run=True)
        else:
            run_id = f"review-supplement-{args.date.replace('-', '')}-{datetime.now().strftime('%H%M%S')}"
            with PipelineLock(args.db, run_id) if not args.assume_pipeline_lock else nullcontext():
                result = collect_review_supplement(args.db, args.date, tables=args.tables, max_sectors=args.max_sectors, out=args.out)
    except Exception as exc:
        print(f"REVIEW_SUPPLEMENT_ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"planned", "completed", "empty"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
