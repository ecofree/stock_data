"""Operator acceptance audit for the P0-P3 remediation sequence."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.capital_flow_health import assess_capital_flow_health
from trade_system.readiness import assess_trade_date_readiness


def _count(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> int:
    try:
        return int(con.execute(sql, params or []).fetchone()[0] or 0)
    except Exception:
        return 0


def _normalize_trade_date(value: str) -> str:
    """Accept both CLI YYYYMMDD and storage YYYY-MM-DD forms."""
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return str(value)[:10]


def _fetchone(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    try:
        return con.execute(sql, params or []).fetchone()
    except Exception:
        return None


def audit(
    db_path: str | Path,
    trade_date: str,
    reports_dir: str | Path = "reports",
    *,
    max_age_seconds: int = 600,
    now: datetime | None = None,
) -> dict:
    trade_date = _normalize_trade_date(trade_date)
    root = Path(reports_dir)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        stock_batch = _fetchone(con,
            "SELECT expected_rows,fetched_rows,coverage_pct,status,updated_at FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)",
            [trade_date],
        )
        sector_batch = _fetchone(con,
            "SELECT expected_rows,fetched_rows,coverage_pct,status,updated_at FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)",
            [trade_date],
        )
        candidates = _count(con, "SELECT count(*) FROM stock_candidate_stage_signal WHERE trade_date=?", [trade_date])
        actionable = _count(con, "SELECT count(*) FROM stock_candidate_stage_signal WHERE trade_date=? AND coalesce(is_actionable,false)", [trade_date])
        stage_columns = {row[1] for row in con.execute("PRAGMA table_info('stock_candidate_stage_signal')").fetchall()}
        execution_column = "is_executable" if "is_executable" in stage_columns else "is_actionable"
        risk_gate = " AND coalesce(risk_approved,false)" if "risk_approved" in stage_columns else ""
        execution_ready = _count(
            con,
            f"SELECT count(*) FROM stock_candidate_stage_signal WHERE trade_date=? AND coalesce({execution_column},false){risk_gate}",
            [trade_date],
        )
        evidence = _count(
            con,
            """SELECT count(*) FROM stock_candidate_stage_signal
               WHERE trade_date=? AND coalesce(is_actionable,false)
                 AND evidence_json LIKE '%multi_source_stock_flow%'""",
            [trade_date],
        )
        outcomes = _count(con, "SELECT count(*) FROM operator_trade_outcome WHERE trade_date=?", [trade_date])
        plans = _count(con, "SELECT count(*) FROM trade_plan WHERE trade_date=?", [trade_date])
        watchlist = _count(con, "SELECT count(*) FROM watchlist WHERE trade_date=?", [trade_date])
        auction_ticks = _count(con, "SELECT count(*) FROM auction_tick WHERE date=CAST(? AS DATE)", [trade_date])
        index_type = _fetchone(con,
            "SELECT data_type FROM information_schema.columns WHERE table_name='index_kline' AND column_name='date'"
        )
        last_audit = _fetchone(con,
            """SELECT run_id, count(*), sum(CASE WHEN status IN ('failed','degraded') THEN 1 ELSE 0 END)
               FROM pipeline_task_audit GROUP BY run_id ORDER BY max(updated_at) DESC LIMIT 1"""
        )
    finally:
        con.close()
    scheduler = {}
    try:
        completed = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        text = completed.stdout
        for name in ("StockData-Auction", "StockData-Intraday", "StockData-DailyClose"):
            scheduler[name] = name in text
    except Exception:
        scheduler = {name: None for name in ("StockData-Auction", "StockData-Intraday", "StockData-DailyClose")}
    report = {
        "trade_date": trade_date,
        # Keep historical audits unambiguous: when omitted, freshness is
        # evaluated at runtime; when supplied, this is the exact audit clock.
        "as_of": now.isoformat() if now is not None else None,
        "p0": {
            "stock_batch": dict(zip(("expected_rows", "fetched_rows", "coverage_pct", "status", "updated_at"), stock_batch or (0, 0, 0, "missing", None))),
            "sector_batch": dict(zip(("expected_rows", "fetched_rows", "coverage_pct", "status", "updated_at"), sector_batch or (0, 0, 0, "missing", None))),
            "intraday_ready": False,
            "scheduler": scheduler,
            "last_pipeline_audit": {"run_id": last_audit[0], "tasks": int(last_audit[1]), "bad_tasks": int(last_audit[2] or 0)} if last_audit else {},
        },
        "p1": {
            "candidate_rows": candidates,
            "actionable_candidates": actionable,
            "execution_ready_candidates": execution_ready,
            "execution_ready_definition": "is_executable AND risk_approved",
            "actionable_with_stock_flow_evidence": evidence,
            "operator_outcomes": outcomes,
            "auction_tick_rows": auction_ticks,
            "auction_hard_red": auction_ticks == 0,
        },
        "p2": {
            "index_kline_date_type": index_type[0] if index_type else "missing",
            "aborted_wal_files": len(list(Path(db_path).resolve().parent.glob(f"{Path(db_path).name}.wal.aborted-*"))),
        },
        "p3": {
            "watchlist_rows": watchlist,
            "trade_plan_rows": plans,
            "daily_review_exists": any(
                (root / name).exists()
                for name in (
                    f"daily_review_{trade_date}_p3.md",
                    f"daily_review_{trade_date}.md",
                )
            ),
        },
    }
    flow_health = assess_capital_flow_health(
        db_path,
        trade_date,
        min_coverage_pct=99.5,
        max_age_seconds=max_age_seconds,
        now=now,
    )
    data_readiness = assess_trade_date_readiness(
        db_path,
        trade_date,
        "intraday",
        max_age_seconds=max_age_seconds,
        now=now,
    )
    report["p0"]["flow_health"] = flow_health
    report["p0"]["data_readiness"] = data_readiness
    report["p0"]["intraday_ready"] = bool(flow_health["ready"] and data_readiness["ready"])
    report["ready_for_manual_use"] = bool(
        report["p0"]["intraday_ready"]
        and execution_ready > 0
        and evidence == actionable
        and report["p2"]["index_kline_date_type"] == "DATE"
        and report["p2"]["aborted_wal_files"] == 0
        and watchlist > 0
        and plans > 0
    )
    return report


def render(report: dict) -> str:
    as_of = report.get("as_of") or "runtime clock"
    lines = ["# P0-P3 Acceptance", "", f"- Trade date: `{report['trade_date']}`", f"- As of: `{as_of}`", f"- Manual-use ready: `{str(report['ready_for_manual_use']).lower()}`", "", "```json", json.dumps(report, ensure_ascii=False, indent=2, default=str), "```", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit P0-P3 operator acceptance gates.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--max-age-seconds", type=int, default=600)
    parser.add_argument(
        "--as-of",
        default="",
        help="Evaluate freshness at this ISO timestamp for audited recovery runs.",
    )
    parser.add_argument("--out", default="reports/p0_p3_acceptance_latest.md")
    args = parser.parse_args()
    result = audit(
        args.db,
        args.date,
        args.reports_dir,
        max_age_seconds=args.max_age_seconds,
        now=datetime.fromisoformat(args.as_of) if args.as_of else None,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(result), encoding="utf-8")
    print(f"ready_for_manual_use={str(result['ready_for_manual_use']).lower()} out={out}")
    return 0 if result["ready_for_manual_use"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
