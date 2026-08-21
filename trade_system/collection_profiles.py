"""Collection profiles for the operator-facing market-data scheduler.

The project has many providers, but a trading-day run should only touch the
sources needed by the current decision window.  This module is deliberately
small and dependency-light: it describes the phase/source matrix and provides
the freshness gate used by ``run_integrated_daily.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import duckdb

from trade_system.ths_quality import canonical_ths_snapshot


PHASES = ("auction", "intraday", "close", "history")


@dataclass(frozen=True)
class ProfileTask:
    name: str
    source: str
    cadence_seconds: int | None
    purpose: str
    network: bool = True


PROFILE_TASKS: dict[str, tuple[ProfileTask, ...]] = {
    "auction": (
        ProfileTask("collect_market_context", "KPL market/rise-fall", 300, "market regime and auction context"),
        ProfileTask("collect_realtime_limit_pool", "KPL L2 realtime/ladder", 180, "same-day executable limit-up pool"),
        ProfileTask("collect_auction_evidence", "KPL auction tick/anomaly", 180, "bounded stock-level auction evidence"),
        ProfileTask("build_auction_evidence", "DuckDB auction snapshot", 60, "normalize stock-level auction evidence", network=False),
    ),
    "intraday": (
        ProfileTask("collect_market_context", "KPL market/rise-fall", 300, "market regime refresh"),
        ProfileTask("collect_realtime_limit_pool", "KPL L2 realtime/ladder", 180, "candidate-pool refresh"),
        ProfileTask("collect_intraday_stock_flow_market", "Eastmoney push2 clist", 300, "full-market stock capital flow"),
        ProfileTask(
            "collect_executable_quotes",
            "Tencent qt.gtimg.cn spot",
            180,
            "candidate-only live prices for entry-executable signals when clist is delayed-only",
        ),
        ProfileTask(
            "collect_l2_focus",
            "KPL /l2/stock-intraday (candidates)",
            300,
            "bounded L2 price curves; phase mode never runs full L2",
        ),
        # Keep the sector snapshot inside the 10-minute readiness window even
        # when L2/quote collection consumes several minutes before the gate.
        ProfileTask("collect_intraday_sector_flow_full", "Eastmoney sector pages + TuShare/THS aggregate", 300, "full-sector capital flow refreshed after L2"),
    ),
    "close": (
        ProfileTask("collect_market_context", "KPL market/rise-fall", 3600, "final market snapshot"),
        ProfileTask("sync_tushare_close", "TuShare relay date batches", 3600, "same-day daily/basic/adjustment/money-flow facts"),
        ProfileTask("refresh_ths_weekly", "THS concept web catalogue/members", 7 * 86400, "weekly 374-concept membership snapshot"),
        ProfileTask("collect_realtime_limit_pool", "KPL L2 then Eastmoney push2ex", 3600, "final limit-up pool"),
        ProfileTask("collect_kpl_stock_flow_focus", "KPL advanced/zjmm-min", 3600, "bounded independent money-flow confirmation for candidate stocks"),
        ProfileTask("collect_intraday_stock_flow_market", "Eastmoney push2 then push2delay + datacenter", 3600, "final stock capital-flow snapshot"),
        ProfileTask("collect_intraday_sector_flow_full", "Eastmoney sector pages + TuShare/THS aggregate", 3600, "final sector capital-flow snapshot"),
        ProfileTask("collect_executable_quotes", "Tencent qt.gtimg.cn spot", 3600, "final candidate live prices; auto-boost if KPL stale"),
        ProfileTask("collect_finance_gapfill", "Eastmoney/Sina financial statements", 7 * 86400, "bounded quarterly gap-fill"),
    ),
    "history": (
        ProfileTask("backfill_2026_tushare", "TuShare relay", None, "resumable daily/basic/moneyflow history"),
        ProfileTask("backfill_2026_ths_concepts", "THS web pages", 7 * 86400, "weekly concept catalogue and constituents snapshot"),
        ProfileTask("run_staged_after_close", "provider fallback graph", None, "financials, statements, margin and historical northbound"),
    ),
}


_WINDOWS = {
    "auction": (time(8, 30), time(9, 30)),
    "intraday": (time(9, 30), time(15, 0)),
    "close": (time(15, 0), time(18, 30)),
}


def resolve_phase(phase: str = "auto", now: datetime | None = None) -> str:
    """Resolve an explicit phase or the local Asia/Shanghai wall-clock phase."""
    value = (phase or "auto").strip().lower()
    if value in PHASES:
        return value
    if value not in {"auto", "full"}:
        raise ValueError(f"unsupported collection phase: {phase}")
    if value == "full":
        return "full"
    current = (now or datetime.now()).time()
    for name, (start, end) in _WINDOWS.items():
        if start <= current < end:
            return name
    # Before auction is pre-open preparation; after close is the close window.
    return "auction" if current < time(9, 30) else "close"


def phase_tasks(phase: str) -> tuple[ProfileTask, ...]:
    if phase not in PROFILE_TASKS:
        raise ValueError(f"unsupported collection phase: {phase}")
    return PROFILE_TASKS[phase]


def phase_matrix() -> list[dict[str, Any]]:
    return [
        {"phase": phase, "tasks": [task.__dict__.copy() for task in tasks]}
        for phase, tasks in PROFILE_TASKS.items()
    ]


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table]
    ).fetchone()[0])


def _column_exists(
    con: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name=? AND column_name=?",
        [table, column],
    ).fetchone()[0])


def _latest(con: duckdb.DuckDBPyConnection, query: str, params: list[Any]) -> tuple[Any, ...] | None:
    try:
        row = con.execute(query, params).fetchone()
        return tuple(row) if row else None
    except Exception:
        return None


def task_due(db_path: str | Path, trade_date: str, task_name: str,
             *, phase: str | None = None, now: datetime | None = None, force: bool = False) -> tuple[bool, str]:
    """Return ``(due, reason)`` without making any network request.

    A partial flow batch is retried on its shorter cadence; a successful batch
    is not fetched again until its TTL expires.  Unknown/local tasks are left
    due so that the gate cannot accidentally suppress a required report.
    """
    if force:
        return True, "forced"
    # Resolve the task within the active phase first so the enforced cadence matches
    # the documented per-phase contract (audit P2 #2: a shared name like
    # collect_market_context must use the close 3600s TTL in the close phase, not the
    # auction 300s that a first-match across all phases would pick).  Fall back to any
    # phase for callers that do not supply one.
    task = None
    if phase and phase in PROFILE_TASKS:
        task = next((item for item in PROFILE_TASKS[phase] if item.name == task_name), None)
    if task is None:
        task = next((item for items in PROFILE_TASKS.values() for item in items if item.name == task_name), None)
    if task is None or not task.network or task.cadence_seconds is None:
        return True, "no freshness gate"
    current = now or datetime.now()
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return True, "database unavailable"
    try:
        row: tuple[Any, ...] | None = None
        if task_name == "collect_market_context":
            sources = []
            if _table_exists(con, "daily_summary"):
                real_filter = (
                    " AND lower(coalesce(source_kind,'real')) "
                    "NOT IN ('fallback','derived_current')"
                    if _column_exists(con, "daily_summary", "source_kind")
                    else ""
                )
                sources.append(
                    "SELECT max(fetched_at) AS fetched_at FROM daily_summary "
                    f"WHERE CAST(date AS VARCHAR)=?{real_filter}"
                )
            if _table_exists(con, "market_rise_fall"):
                real_filter = (
                    " AND lower(coalesce(source_kind,'real')) "
                    "NOT IN ('fallback','derived_current')"
                    if _column_exists(con, "market_rise_fall", "source_kind")
                    else ""
                )
                sources.append(
                    "SELECT max(updated_at) AS fetched_at FROM market_rise_fall "
                    f"WHERE CAST(date AS VARCHAR)=?{real_filter}"
                )
            if not sources:
                return True, "market context tables missing"
            union = " UNION ALL ".join(sources)
            row = _latest(con, f"SELECT max(fetched_at), 'ok' FROM ({union})", [trade_date] * len(sources))
        elif task_name == "collect_realtime_limit_pool":
            if not _table_exists(con, "realtime_candidate_pool_snapshot"):
                return True, "candidate snapshot missing"
            row = _latest(con, "SELECT fetched_at, status FROM realtime_candidate_pool_snapshot WHERE CAST(trade_date AS VARCHAR)=? ORDER BY fetched_at DESC LIMIT 1", [trade_date])
        elif task_name == "collect_auction_evidence":
            if not _table_exists(con, "auction_collection_batch"):
                return True, "auction batch checkpoint missing"
            row = _latest(con, "SELECT attempted_at, status FROM auction_collection_batch WHERE CAST(trade_date AS VARCHAR)=?", [trade_date])
        elif task_name == "refresh_ths_weekly":
            if not _table_exists(con, "history_fetch_checkpoint"):
                return True, "THS history checkpoint missing"
            if all(_table_exists(con, name) for name in (
                "ths_concept_daily",
                "ths_concept_stock_history",
                "ths_concept_member_checkpoint",
            )):
                canonical = canonical_ths_snapshot(
                    con, trade_date, minimum_concepts=374
                )
                if canonical is None:
                    return True, "canonical THS snapshot missing or incomplete"
                fetched = datetime.fromisoformat(canonical["snapshot_date"])
                age = max(0.0, (current.replace(tzinfo=None) - fetched).total_seconds())
                if age < task.cadence_seconds:
                    return False, f"fresh canonical snapshot age={int(age)}s ttl={task.cadence_seconds}s"
                return True, f"expired canonical snapshot age={int(age)}s ttl={task.cadence_seconds}s"
            row = _latest(
                con,
                "SELECT updated_at, status FROM history_fetch_checkpoint "
                "WHERE dataset='ths_concept_snapshot' "
                "ORDER BY updated_at DESC LIMIT 1",
                [],
            )
        elif task_name == "sync_tushare_close":
            if not _table_exists(con, "history_fetch_checkpoint"):
                return True, "TuShare history checkpoint missing"
            required = ("daily", "daily_basic", "adj_factor", "moneyflow", "industry_flow")
            placeholders = ",".join("?" for _ in required)
            row = _latest(
                con,
                f"""
                SELECT
                    max(updated_at),
                    CASE
                        WHEN count(DISTINCT dataset)=?
                         AND min(CASE WHEN status='success' AND rows_written>0 THEN 1 ELSE 0 END)=1
                        THEN 'success'
                        ELSE 'partial'
                    END
                FROM history_fetch_checkpoint
                WHERE CAST(trade_date AS VARCHAR)=?
                  AND page_no=0
                  AND dataset IN ({placeholders})
                """,
                [len(required), trade_date, *required],
            )
        elif task_name == "collect_intraday_stock_flow_market":
            if not _table_exists(con, "intraday_stock_flow_batch"):
                return True, "stock-flow batch missing"
            row = _latest(con, "SELECT updated_at, status FROM intraday_stock_flow_batch WHERE CAST(trade_date AS VARCHAR)=? ORDER BY updated_at DESC LIMIT 1", [trade_date])
        elif task_name == "collect_intraday_sector_flow_full":
            if not _table_exists(con, "intraday_sector_flow_batch"):
                return True, "sector-flow batch missing"
            row = _latest(con, "SELECT updated_at, status FROM intraday_sector_flow_batch WHERE CAST(trade_date AS VARCHAR)=? ORDER BY updated_at DESC LIMIT 1", [trade_date])
        elif task_name == "collect_finance_gapfill":
            if not _table_exists(con, "finance_fetch_checkpoint"):
                return True, "finance checkpoint missing"
            row = _latest(con, "SELECT max(fetched_at), count(*) FROM finance_fetch_checkpoint WHERE CAST(target_date AS VARCHAR)=?", [trade_date])
        if not row or row[0] is None:
            return True, "no same-date snapshot"
        fetched = row[0]
        if not isinstance(fetched, datetime):
            fetched = datetime.fromisoformat(str(fetched).replace("Z", "+00:00")).replace(tzinfo=None)
        status = str(row[1]).lower() if len(row) > 1 and row[1] is not None else ""
        ttl = task.cadence_seconds
        if task_name == "refresh_ths_weekly":
            # A successful THS snapshot is weekly.  Interrupted/partial
            # snapshots are not successful freshness and must retry on the
            # next close with the resumable checkpoint, instead of freezing
            # the project for another seven days.
            if status in {"partial", "failed", "empty", "stale", "error", "running"}:
                ttl = max(60, min(ttl // 2, 86400))
        elif status in {"partial", "failed", "empty", "stale", "error", "running"}:
            # Slow weekly sources are normally fetched once per week, but a
            # failed refresh should retry at the next daily close instead of
            # remaining broken for another 3.5 days.  ``running`` is included
            # because an interrupted crawl (killed process) leaves a running
            # checkpoint behind; without this it would be treated as fresh
            # for the full TTL and never retried (observed 2026-08-10 THS).
            ttl = max(60, min(ttl // 2, 86400))
        age = max(0.0, (current.replace(tzinfo=None) - fetched).total_seconds())
        if age < ttl:
            return False, f"fresh age={int(age)}s ttl={ttl}s status={status or 'ok'}"
        return True, f"expired age={int(age)}s ttl={ttl}s status={status or 'ok'}"
    finally:
        con.close()


def render_matrix() -> str:
    lines = ["# Collection phase/source matrix", "", "| phase | task | source | cadence | purpose |", "|---|---|---|---:|---|"]
    for phase, tasks in PROFILE_TASKS.items():
        for task in tasks:
            cadence = "manual/checkpoint" if task.cadence_seconds is None else f"{task.cadence_seconds}s"
            lines.append(f"| {phase} | {task.name} | {task.source} | {cadence} | {task.purpose} |")
    lines.extend([
        "", "## Rules", "",
        "- auction: only KPL market context, realtime limit pool and local auction evidence.",
        "- intraday: only realtime market/stock-flow/sector-flow; partial coverage is retained and retried on a shorter TTL.",
        "- close: final snapshots plus bounded financial gap-fill; no historical backfill, QLib or news fan-out.",
        "- history: manual/off-hours only; every history collector keeps its own checkpoint.",
    ])
    return "\n".join(lines) + "\n"
