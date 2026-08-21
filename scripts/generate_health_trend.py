"""Generate the rolling data-health trend report (read-only).

Aggregates the operational history that already exists in the database —
``_collect_log`` (per-endpoint collection outcomes) and
``data_quality_event`` (gate failures) — into a markdown trend report so
degradation is visible across days instead of only within one run.

Output: ``reports/data_health_trend_latest.md``.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.db_utils import fetch_dicts  # noqa: E402


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?", [name]
    ).fetchone()
    return bool(row and row[0])


def collect_log_trend(con: duckdb.DuckDBPyConnection, days: int) -> list[dict[str, Any]]:
    if not _table_exists(con, "_collect_log"):
        return []
    return fetch_dicts(
        con,
        """
        SELECT CAST(fetched_at AS DATE) AS day,
               count(*) AS calls,
               sum(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_calls,
               round(100.0 * sum(CASE WHEN status='ok' THEN 1 ELSE 0 END) / count(*), 2) AS ok_pct,
               sum(rows_inserted) AS rows_inserted
        FROM _collect_log
        WHERE fetched_at >= current_timestamp - INTERVAL (?) DAY
        GROUP BY 1 ORDER BY 1 DESC
        """,
        [days],
    )


def worst_endpoints(con: duckdb.DuckDBPyConnection, days: int, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(con, "_collect_log"):
        return []
    return fetch_dicts(
        con,
        """
        SELECT table_name, endpoint,
               count(*) AS calls,
               sum(CASE WHEN status<>'ok' THEN 1 ELSE 0 END) AS failures,
               max(CAST(fetched_at AS DATE)) AS last_seen
        FROM _collect_log
        WHERE fetched_at >= current_timestamp - INTERVAL (?) DAY
          AND status <> 'ok'
        GROUP BY 1, 2
        ORDER BY failures DESC, last_seen DESC
        LIMIT ?
        """,
        [days, limit],
    )


def quality_events(con: duckdb.DuckDBPyConnection, days: int) -> list[dict[str, Any]]:
    if not _table_exists(con, "data_quality_event"):
        return []
    return fetch_dicts(
        con,
        """
        SELECT CAST(trade_date AS VARCHAR) AS trade_date, severity, code,
               count(*) AS events
        FROM data_quality_event
        WHERE observed_at >= current_timestamp - INTERVAL (?) DAY
        GROUP BY 1, 2, 3
        ORDER BY 1 DESC, events DESC
        LIMIT 30
        """,
        [days],
    )


def render_report(
    trend: list[dict[str, Any]],
    endpoints: list[dict[str, Any]],
    events: list[dict[str, Any]],
    days: int,
) -> str:
    lines = [
        "# Data health trend",
        "",
        f"Window: last {days} day(s). Generated {date.today().isoformat()}. "
        "Read-only aggregation of `_collect_log` and `data_quality_event`.",
        "",
        "## Daily collection success rate",
        "",
        "| day | calls | ok | ok% | rows inserted |",
        "|---|---|---|---|---|",
    ]
    for row in trend:
        lines.append(
            f"| {row['day']} | {row['calls']} | {row['ok_calls']} "
            f"| {row['ok_pct']} | {row['rows_inserted']} |"
        )
    if not trend:
        lines.append("| (no _collect_log data) | | | | |")

    lines += [
        "",
        "## Failing endpoints (worst first)",
        "",
        "| table | endpoint | calls | failures | last seen |",
        "|---|---|---|---|---|",
    ]
    for row in endpoints:
        lines.append(
            f"| {row['table_name']} | {row['endpoint']} | {row['calls']} "
            f"| {row['failures']} | {row['last_seen']} |"
        )
    if not endpoints:
        lines.append("| (no failures in window) | | | | |")

    lines += [
        "",
        "## Data-quality gate events",
        "",
        "| trade_date | severity | code | events |",
        "|---|---|---|---|",
    ]
    for row in events:
        lines.append(
            f"| {row['trade_date']} | {row['severity']} | {row['code']} | {row['events']} |"
        )
    if not events:
        lines.append("| (none recorded in window) | | | | |")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument(
        "--out", default=str(PROJECT_ROOT / "reports" / "data_health_trend_latest.md")
    )
    args = parser.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    try:
        report = render_report(
            collect_log_trend(con, args.days),
            worst_endpoints(con, args.days, limit=15),
            quality_events(con, args.days),
            args.days,
        )
    finally:
        con.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"health trend report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
