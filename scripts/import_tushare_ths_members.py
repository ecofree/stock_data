"""Import an audited TuShare ``ths_member`` snapshot into the THS tables.

The input is produced by the resumable fetch script.  It is deliberately an
import step: no token is stored in the repository and each row retains the
provider in ``source``/``raw_json``.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb
import pandas as pd

from schema import init_schema
from trade_system.ths_history import render_report


def iso(value: str) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--input", default="tmp/tushare_ths_members.json")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--report", default="reports/ths_2026_concepts_latest.md")
    args = parser.parse_args()
    trade_date = iso(args.date)
    state = json.loads(Path(args.input).read_text(encoding="utf8"))
    mappings = {row["ths_code"]: row for row in state.get("mappings", [])}
    missing = {row["ths_code"]: row for row in state.get("missing", [])}
    catalog = [(code, row["ths_name"]) for code, row in mappings.items()]
    catalog.extend((code, row["ths_name"]) for code, row in missing.items())
    catalog.sort(key=lambda x: x[0])

    con = duckdb.connect(args.db)
    init_schema(con)
    old_date = con.execute("SELECT max(trade_date) FROM ths_concept_stock_history").fetchone()[0]
    old_members = {}
    old_pages = {}
    if old_date:
        for code, name, stock_code, stock_name in con.execute(
            "SELECT concept_code,concept_name,stock_code,stock_name FROM ths_concept_stock_history WHERE trade_date=?",
            [old_date],
        ).fetchall():
            old_members.setdefault(code, []).append({"con_code": stock_code, "con_name": stock_name})
        for code, expected, fetched in con.execute(
            "SELECT concept_code,pages_expected,pages_fetched FROM ths_concept_member_checkpoint WHERE trade_date=?",
            [old_date],
        ).fetchall():
            old_pages[code] = (int(expected or 0), int(fetched or 0))
    con.execute("DELETE FROM ths_concept_daily WHERE trade_date=?", [trade_date])
    con.execute("DELETE FROM ths_concept_stock_history WHERE trade_date=?", [trade_date])
    con.execute("DELETE FROM ths_concept_member_checkpoint WHERE trade_date=?", [trade_date])
    con.commit()

    # Preserve the 4 boards not present in TuShare's index from the latest
    # guarded THS HTML run.  They remain partial and are explicitly reported.
    all_concepts = []
    all_members = []
    checkpoint_rows = []
    for rank, (ths_code, concept_name) in enumerate(catalog, 1):
        mapping = mappings.get(ths_code)
        provider = "tushare_ths_member"
        rows = []
        expected_pages = fetched_pages = 1
        error = ""
        if mapping:
            rows = state.get("members", {}).get(mapping["ts_code"], [])
            if not rows:
                provider = "tushare_ths_member"
                error = "TuShare ths_member returned no rows"
        else:
            provider = "ths_html_partial"
            expected_pages, _old_fetched = old_pages.get(f"THS-{ths_code}", (0, 0))
            fetched_pages = min(5, expected_pages) if expected_pages else 0
            rows = old_members.get(f"THS-{ths_code}", [])
            error = f"Tushare ths_index missing concept; THS HTML pagination partial {fetched_pages}/{expected_pages}"

        deduped = {}
        for row in rows:
            code = str(row.get("con_code") or row.get("code") or "").upper()
            if "." in code:
                code = code.split(".")[0]
            code = code[-6:] if code[-6:].isdigit() else ""
            if code:
                deduped[code] = str(row.get("con_name") or row.get("name") or "").strip()
        members = list(deduped.items())
        raw = {"requested_date": trade_date, "fetched_date": date.today().isoformat(),
               "date_verified": False, "provider": provider, "ths_concept_id": ths_code,
               "tushare_ts_code": mapping.get("ts_code") if mapping else None,
               "pages_expected": expected_pages, "pages_fetched": fetched_pages,
               "error": error}
        all_concepts.append((trade_date, f"THS-{ths_code}", concept_name, rank, len(members), provider,
                             json.dumps(raw, ensure_ascii=False, separators=(",", ":")), False))
        for member_rank, (code, name) in enumerate(members, 1):
            all_members.append((trade_date, f"THS-{ths_code}", concept_name, code, name, member_rank,
                                provider, json.dumps({**raw, "member_rank": member_rank}, ensure_ascii=False,
                                                     separators=(",", ":")), False))
        status = "success" if members and fetched_pages >= expected_pages else ("empty" if not members else "partial")
        checkpoint_rows.append((trade_date, f"THS-{ths_code}", concept_name, status, expected_pages,
                                fetched_pages, len(members), 1, error, datetime.now()))

    def insert_chunks(sql: str, rows: list[tuple], size: int = 1000) -> None:
        for start in range(0, len(rows), size):
            con.executemany(sql, rows[start:start + size])
            con.commit()

    insert_chunks("INSERT INTO ths_concept_daily (trade_date,concept_code,concept_name,rank,stock_count,source,raw_json,date_verified) VALUES (?,?,?,?,?,?,?,?)", all_concepts)
    # DuckDB's vectorized DataFrame path is materially faster and uses far
    # less temporary memory than a single 65k-row executemany call on Windows.
    member_columns = ["trade_date", "concept_code", "concept_name", "stock_code", "stock_name",
                      "concept_rank", "source", "raw_json", "date_verified"]
    member_frame = pd.DataFrame(all_members, columns=member_columns)
    con.register("_ths_members_import", member_frame)
    con.execute("INSERT INTO ths_concept_stock_history (trade_date,concept_code,concept_name,stock_code,stock_name,concept_rank,source,raw_json,date_verified) SELECT trade_date,concept_code,concept_name,stock_code,stock_name,concept_rank,source,raw_json,date_verified FROM _ths_members_import")
    con.unregister("_ths_members_import")
    con.commit()
    insert_chunks("INSERT INTO ths_concept_member_checkpoint (trade_date,concept_code,concept_name,status,pages_expected,pages_fetched,member_rows,attempts,last_error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", checkpoint_rows)
    success = sum(1 for row in checkpoint_rows if row[3] == "success")
    partial = sum(1 for row in checkpoint_rows if row[3] == "partial")
    overall = "success" if success == len(catalog) else "partial"
    con.execute("INSERT INTO history_fetch_checkpoint(dataset,trade_date,page_no,status,rows_written,attempts,last_error,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(dataset,trade_date,page_no) DO UPDATE SET status=excluded.status,rows_written=excluded.rows_written,attempts=excluded.attempts,last_error=excluded.last_error,updated_at=excluded.updated_at", ["ths_concept_snapshot", trade_date, 0, overall, len(all_concepts) + len(all_members), 1, f"tushare_exact={success}; partial={partial}", datetime.now()])
    con.commit()
    con.close()
    result = {"start_date": trade_date, "end_date": trade_date, "historical_supported": False,
              "missing_historical_dates": [], "snapshot": {"trade_date": trade_date, "status": overall,
              "mode": "full", "catalog_count": len(catalog), "concept_rows": len(all_concepts),
              "member_rows": len(all_members), "failed_concepts": 0, "partial_member_concepts": partial,
              "missing_member_concepts": sum(1 for row in checkpoint_rows if row[6] == 0)}}
    report = render_report(args.db, result, args.report)
    print(json.dumps({"status": overall, "concepts": len(all_concepts), "members": len(all_members),
                      "exact_tushare": success, "partial_fallback": partial, "report": str(report)}, ensure_ascii=False))
    return 0 if overall == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
