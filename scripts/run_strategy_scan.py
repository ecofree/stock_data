from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.quality import table_columns, table_exists
from trade_system.strategy.definition import load_default_stage_strategies
from trade_system.strategy.engine import run_strategy_scan
from trade_system.strategy.schema import (
    install_strategy_tables,
    persist_strategy_definitions,
    persist_strategy_scan_results,
)


def _fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def load_operator_candidates(db_path: str | Path) -> list[dict]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if table_exists(con, "v_operator_candidates"):
            return _fetch_dicts(
                con,
                """
                SELECT trade_date, stage, stock_code, stock_name, score, decision, data_origin
                FROM v_operator_candidates
                WHERE stock_code IS NOT NULL
                """,
            )
        if table_exists(con, "stock_candidate_stage_signal"):
            actionable_filter = (
                "AND coalesce(is_actionable, false) = true"
                if "is_actionable" in table_columns(con, "stock_candidate_stage_signal")
                else ""
            )
            return _fetch_dicts(
                con,
                f"""
                SELECT trade_date, stage, stock_code, stock_name, score, decision
                FROM stock_candidate_stage_signal
                WHERE stock_code IS NOT NULL
                {actionable_filter}
                """,
            )
        return []
    finally:
        con.close()


def render_strategy_scan_report(results: list[dict], candidate_count: int) -> str:
    counts = Counter(row["stage"] for row in results)
    lines = [
        "# Strategy Scan",
        "",
        f"- Input candidates: `{candidate_count}`",
        f"- Matched results: `{len(results)}`",
        "",
        "| Stage | Matches |",
        "|---|---:|",
    ]
    for stage, count in sorted(counts.items()):
        lines.append(f"| {stage} | {count} |")
    lines.extend(["", "## Latest Results", "", "| Date | Stage | Symbol | Strategy | Score | Reason |", "|---|---|---|---|---:|---|"])
    for row in results[:100]:
        lines.append(
            f"| {row['trade_date']} | {row['stage']} | {row['symbol']} | {row['strategy_id']} | "
            f"{float(row['score'] or 0):.2f} | {row['selected_reason']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run tickflow-style strategy scan over operator candidates.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "strategy_scan_latest.md"))
    args = parser.parse_args()

    install_strategy_tables(args.db)
    strategies = load_default_stage_strategies()
    persist_strategy_definitions(args.db, strategies)
    candidates = load_operator_candidates(args.db)
    results = run_strategy_scan(candidates, strategies)
    persisted = persist_strategy_scan_results(args.db, results)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_strategy_scan_report(results, len(candidates)), encoding="utf-8")
    print(f"strategy_scan_report={out}")
    print(f"input_candidates={len(candidates)}")
    print(f"strategy_scan_results={persisted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
