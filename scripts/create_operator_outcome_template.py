from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.operator_outcomes import OUTCOME_COLUMNS, ensure_operator_outcome_tables
from trade_system.quality import table_exists


def _latest_date(con: duckdb.DuckDBPyConnection) -> str:
    for relation, column in (
        ("trade_plan", "trade_date"),
        ("stock_candidate_stage_signal", "trade_date"),
        ("stock_candidate_score", "trade_date"),
    ):
        if table_exists(con, relation):
            value = con.execute(f"SELECT max({column}) FROM {relation}").fetchone()[0]
            if value:
                return str(value)[:10]
    return date.today().isoformat()


def create_template(db_path: str | Path, output: str | Path, trade_date: str | None = None) -> dict[str, int | str]:
    ensure_operator_outcome_tables(db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        selected_date = trade_date or _latest_date(con)
        rows: list[tuple[str, str, str]] = []
        if table_exists(con, "trade_plan"):
            rows = con.execute(
                "SELECT trade_date, stock_code, coalesce(stock_name, '') "
                "FROM trade_plan WHERE trade_date=? ORDER BY stock_code",
                [selected_date],
            ).fetchall()
        if not rows and table_exists(con, "v_operator_candidates"):
            rows = con.execute(
                "SELECT trade_date, stock_code, coalesce(stock_name, '') "
                "FROM v_operator_candidates WHERE trade_date=? ORDER BY score DESC NULLS LAST, stock_code LIMIT 100",
                [selected_date],
            ).fetchall()
        if not rows and table_exists(con, "stock_candidate_score"):
            rows = con.execute(
                "SELECT trade_date, stock_code, coalesce(stock_name, '') "
                "FROM stock_candidate_score WHERE trade_date=? "
                "ORDER BY score DESC NULLS LAST, stock_code LIMIT 100",
                [selected_date],
            ).fetchall()
        if not rows and table_exists(con, "stock_candidate_stage_signal"):
            # The stage signal table is the production candidate source when
            # close-stage trade_plan generation is blocked or has not yet run.
            # Export only actionable rows, preserving the evidence-gated
            # distinction between review candidates and orders.
            rows = con.execute(
                "SELECT trade_date, stock_code, coalesce(stock_name, '') "
                "FROM stock_candidate_stage_signal "
                "WHERE CAST(trade_date AS VARCHAR)=? AND coalesce(is_actionable, false)=true "
                "ORDER BY score DESC NULLS LAST, stock_code LIMIT 100",
                [selected_date],
            ).fetchall()
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTCOME_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "trade_date": str(row[0])[:10],
                    "stock_code": str(row[1]),
                    "stock_name": str(row[2] or ""),
                    "execution_status": "review_required",
                    "outcome_tag": "unclassified",
                    "mistake_tag": "unreviewed",
                    "imported_from": path.name,
                })
        return {"trade_date": selected_date, "rows": len(rows), "output": str(path)}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a human-review CSV template; this does not create outcomes.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", dest="trade_date", help="Trade date (YYYY-MM-DD). Defaults to latest plan/candidate date.")
    parser.add_argument("--out", default="reports/operator_outcomes_template.csv")
    args = parser.parse_args()
    result = create_template(args.db, args.out, args.trade_date)
    print(f"trade_date={result['trade_date']} rows={result['rows']} output={result['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
