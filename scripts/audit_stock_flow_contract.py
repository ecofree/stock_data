"""Report comparable-provider conflicts in the canonical stock-flow table."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--out", default="reports/stock_flow_contract_audit_latest.md")
    parser.add_argument("--date", default="")
    args = parser.parse_args()
    con = duckdb.connect(str(args.db), read_only=True)
    where = "WHERE NOT coalesce(is_stale,FALSE) AND main_net IS NOT NULL"
    params: list[str] = []
    if args.date:
        where += " AND source_date=CAST(? AS DATE)"
        params.append(args.date)
    summary = con.execute(
        f"""WITH x AS (
                SELECT source_date,stock_code,
                       string_agg(DISTINCT provider, ',' ORDER BY provider) providers,
                       string_agg(DISTINCT coalesce(flow_definition,'missing'), ',' ORDER BY coalesce(flow_definition,'missing')) definitions,
                       count(DISTINCT provider) provider_count,
                       count(DISTINCT coalesce(flow_definition,'missing')) definition_count,
                       min(main_net) min_main,max(main_net) max_main
                FROM multi_source_stock_flow {where}
                GROUP BY source_date,stock_code
                HAVING count(DISTINCT provider)>1
             )
             SELECT count(*),
                    count(*) FILTER (WHERE definition_count=1),
                    count(*) FILTER (WHERE definition_count>1),
                    count(*) FILTER (WHERE definition_count=1 AND abs(max_main-min_main)>greatest(10000.0,abs(max_main)*0.01)),
                    coalesce(max(abs(max_main-min_main)),0)
             FROM x""", params,
    ).fetchone()
    conflicts = con.execute(
        f"""WITH x AS (
                SELECT source_date,stock_code,
                       string_agg(DISTINCT provider, ',' ORDER BY provider) providers,
                       string_agg(DISTINCT coalesce(flow_definition,'missing'), ',' ORDER BY coalesce(flow_definition,'missing')) definitions,
                       min(main_net) min_main,max(main_net) max_main
                FROM multi_source_stock_flow {where}
                GROUP BY source_date,stock_code
                HAVING count(DISTINCT provider)>1 AND count(DISTINCT coalesce(flow_definition,'missing'))=1
             )
             SELECT source_date,stock_code,providers,definitions,min_main,max_main,abs(max_main-min_main) gap
             FROM x
             WHERE abs(max_main-min_main)>greatest(10000.0,abs(max_main)*0.01)
             ORDER BY gap DESC LIMIT 50""", params,
    ).fetchall()
    con.close()
    report = Path(args.out)
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Stock Flow Contract Audit",
        "",
        f"- Date filter: `{args.date or 'all'}`",
        "- Comparable means the same `flow_definition`; different provider definitions are reported separately and are not silently reconciled.",
        "",
        "| Overlap keys | Comparable keys | Cross-definition keys | Comparable conflicts | Max gap |",
        "|---:|---:|---:|---:|---:|",
        f"| {summary[0]} | {summary[1]} | {summary[2]} | {summary[3]} | {summary[4]} |",
        "",
        "## Comparable conflicts",
        "",
        "| Date | Stock | Providers | Definition | Min Main | Max Main | Gap |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    lines.extend(f"| {date} | {code} | {providers} | {definitions} | {min_main} | {max_main} | {gap} |"
                 for date, code, providers, definitions, min_main, max_main, gap in conflicts)
    if not conflicts:
        lines.append("| none | | | | | | |")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report={report} overlap={summary[0]} comparable_conflicts={summary[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
