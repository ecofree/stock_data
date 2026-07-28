"""Upgrade and audit the canonical stock-flow field contract."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DB_PATH
from trade_system.flow_contract import ensure_stock_flow_contract, migrate_existing_stock_flow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--report", default="reports/stock_flow_contract_migration_latest.md")
    args = parser.parse_args()
    db_path = Path(args.db)
    con = duckdb.connect(str(db_path))
    try:
        ensure_stock_flow_contract(con)
        result = migrate_existing_stock_flow(con)
        rows = con.execute(
            """SELECT provider, coalesce(flow_definition,'missing'),
                      count(*), count(*) FILTER (WHERE main_net IS NULL),
                      count(*) FILTER (WHERE net_total IS NULL)
               FROM multi_source_stock_flow
               GROUP BY provider, coalesce(flow_definition,'missing')
               ORDER BY 1, 2"""
        ).fetchall()
    finally:
        con.close()

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Stock Flow Contract Migration",
        "",
        "- Contract: `stock_flow_v2`",
        "- Original raw payloads were retained; canonical TuShare `main_net` is super-large + large.",
        f"- Legacy TuShare rows recalculated: `{result['legacy_tushare_rows']}`",
        "",
        "| Provider | Definition | Rows | Missing Main | Missing Total |",
        "|---|---|---:|---:|---:|",
    ]
    lines.extend(f"| {provider} | {definition} | {count} | {missing_main} | {missing_total} |"
                 for provider, definition, count, missing_main, missing_total in rows)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"db={db_path} report={report} legacy_tushare_rows={result['legacy_tushare_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
