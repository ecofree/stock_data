"""Strict rolling acceptance for the five-session P0 observation window."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_exists
from trade_system.ths_quality import canonical_ths_snapshot
from trade_system.trading_calendar import open_session_dates


PHASES = ("auction", "intraday", "close")
TUSHARE_CLOSE_DATASETS = (
    "daily",
    "daily_basic",
    "adj_factor",
    "moneyflow",
    "industry_flow",
)
GOOD_STOCK_BATCH = {"success", "success_with_unavailable", "partial"}
GOOD_SECTOR_BATCH = {"success", "success_with_optional_gap", "partial"}


def _infer_phase(manifest: dict[str, Any]) -> str | None:
    phase = str(manifest.get("phase") or "").lower()
    if phase in PHASES:
        return phase
    names = {str(step.get("name") or "") for step in manifest.get("steps") or []}
    if names & {"collect_auction_evidence", "generate_auction_stage_signals"}:
        return "auction"
    if names & {"collect_intraday_stock_flow_market", "generate_intraday_stage_signals"}:
        return "intraday"
    if names & {"sync_tushare_close", "generate_close_stage_signals", "generate_daily_review"}:
        return "close"
    return None


def _latest_manifests(reports_dir: str | Path) -> dict[tuple[str, str], dict[str, Any]]:
    root = Path(reports_dir).resolve() / "runs"
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not root.exists():
        return out
    for path in root.glob("*/run.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        trade_date = str(item.get("trade_date") or "")[:10]
        phase = _infer_phase(item)
        if not trade_date or phase not in PHASES:
            continue
        item["_run_dir"] = str(path.parent)
        key = (trade_date, phase)
        stamp = str(item.get("completed_at") or item.get("started_at") or "")
        old_stamp = str(
            out.get(key, {}).get("completed_at")
            or out.get(key, {}).get("started_at")
            or ""
        )
        if key not in out or stamp >= old_stamp:
            out[key] = item
    return out


def _sessions(
    con: duckdb.DuckDBPyConnection,
    as_of: str,
    required_days: int,
) -> list[dict[str, Any]]:
    target = date.fromisoformat(as_of)
    calendar = {
        date.fromisoformat(item): True
        for item in open_session_dates(con, "1900-01-01", as_of)
    }
    sessions: list[dict[str, Any]] = []
    cursor = target
    while len(sessions) < required_days and cursor >= target - timedelta(days=45):
        if cursor in calendar:
            if calendar[cursor]:
                sessions.append({"trade_date": cursor.isoformat(), "calendar_verified": True})
        cursor -= timedelta(days=1)
    return list(reversed(sessions))


def _fetchone(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any],
) -> tuple[Any, ...] | None:
    try:
        row = con.execute(sql, params).fetchone()
        return tuple(row) if row else None
    except Exception:
        return None


def _batch_status(
    con: duckdb.DuckDBPyConnection,
    table: str,
    trade_date: str,
    good_statuses: set[str],
) -> dict[str, Any]:
    row = _fetchone(
        con,
        f"SELECT expected_rows,fetched_rows,coverage_pct,status,updated_at "
        f"FROM {table} WHERE trade_date=CAST(? AS DATE)",
        [trade_date],
    )
    expected, fetched, coverage, status, updated_at = row or (0, 0, 0, "missing", None)
    passed = (
        int(expected or 0) > 0
        and float(coverage or 0) >= 99.5
        and str(status or "").lower() in good_statuses
    )
    return {
        "expected": int(expected or 0),
        "fetched": int(fetched or 0),
        "coverage_pct": float(coverage or 0),
        "status": str(status or "missing"),
        "updated_at": str(updated_at) if updated_at is not None else None,
        "passed": passed,
    }


def _tushare_status(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    if not table_exists(con, "history_fetch_checkpoint"):
        return {"passed": False, "datasets": {}, "missing": list(TUSHARE_CLOSE_DATASETS)}
    rows = con.execute(
        "SELECT dataset,status,rows_written,updated_at "
        "FROM history_fetch_checkpoint "
        "WHERE trade_date=CAST(? AS DATE) AND page_no=0 AND dataset IN (?,?,?,?,?)",
        [trade_date, *TUSHARE_CLOSE_DATASETS],
    ).fetchall()
    datasets = {
        str(row[0]): {
            "status": str(row[1] or ""),
            "rows_written": int(row[2] or 0),
            "updated_at": str(row[3]) if row[3] is not None else None,
        }
        for row in rows
    }
    missing = [
        name
        for name in TUSHARE_CLOSE_DATASETS
        if name not in datasets
        or datasets[name]["status"] != "success"
        or datasets[name]["rows_written"] <= 0
    ]
    return {"passed": not missing, "datasets": datasets, "missing": missing}


def _ths_status(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    minimum_concepts: int | None,
) -> dict[str, Any]:
    canonical = canonical_ths_snapshot(con, trade_date, minimum_concepts=minimum_concepts)
    if canonical:
        snapshot_date = canonical["snapshot_date"]
        age_days = canonical["age_days"]
        passed = 0 <= age_days <= 7
        return {
            "passed": passed,
            "snapshot_date": snapshot_date,
            "age_days": age_days,
            "status": "success" if passed else "canonical_stale",
            "raw_status": "success",
            "concepts": canonical["concepts"],
            "members": canonical["members"],
            "rows_written": canonical["members"],
            "checkpoint_total": canonical["checkpoint_total"],
            "checkpoint_success": canonical["checkpoint_success"],
        }
    # A raw checkpoint or a partially verified default view is not a usable
    # snapshot.  Keep the diagnostics, but fail closed until one global
    # snapshot passes the shared contract.
    checkpoint = _fetchone(
        con,
        "SELECT trade_date,status,rows_written,updated_at "
        "FROM history_fetch_checkpoint "
        "WHERE dataset='ths_concept_snapshot' AND trade_date<=CAST(? AS DATE) "
        "ORDER BY trade_date DESC,updated_at DESC LIMIT 1",
        [trade_date],
    )
    if not checkpoint:
        return {
            "passed": False,
            "snapshot_date": None,
            "status": "missing",
            "raw_status": "missing",
            "concepts": 0,
            "members": 0,
        }
    snapshot_date = str(checkpoint[0])[:10]
    concepts = int(
        (_fetchone(
            con,
            "SELECT count(DISTINCT concept_code) FROM ths_concept_daily "
            "WHERE trade_date=CAST(? AS DATE)",
            [snapshot_date],
        ) or (0,))[0]
        or 0
    )
    members = int(
        (_fetchone(
            con,
            "SELECT count(*) FROM ths_concept_stock_history "
            "WHERE trade_date=CAST(? AS DATE)",
            [snapshot_date],
        ) or (0,))[0]
        or 0
    )
    age_days = (date.fromisoformat(trade_date) - date.fromisoformat(snapshot_date)).days
    status = str(checkpoint[1] or "")
    # The checkpoint is retained for diagnostics only.  It may say success
    # even when rows were written across multiple fetch dates or only a
    # subset is date-verified; canonical_ths_snapshot is the sole authority.
    passed = False
    return {
        "passed": passed,
        "snapshot_date": snapshot_date,
        "age_days": age_days,
        "status": "canonical_incomplete" if status == "success" else status,
        "raw_status": status,
        "concepts": concepts,
        "members": members,
        "rows_written": int(checkpoint[2] or 0),
        "updated_at": str(checkpoint[3]) if checkpoint[3] is not None else None,
    }


def audit_five_day_observation(
    db_path: str | Path,
    reports_dir: str | Path,
    as_of: str,
    *,
    required_days: int = 5,
    minimum_ths_concepts: int | None = None,
) -> dict[str, Any]:
    manifests = _latest_manifests(reports_dir)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        sessions = _sessions(con, as_of, max(1, int(required_days)))
        daily = []
        for session in sessions:
            trade_date = session["trade_date"]
            phases = {}
            for phase in PHASES:
                manifest = manifests.get((trade_date, phase))
                status = str((manifest or {}).get("status") or "missing")
                phases[phase] = {
                    "run_id": (manifest or {}).get("run_id"),
                    "status": status,
                    # Optional post-close capabilities (currently northbound
                    # historical net-buy) may be unavailable without invalidating
                    # the day's core close run.  Only the explicit warning status
                    # is accepted here; degraded/blocked/chain-failure runs stay
                    # fail-closed.
                    "passed": status in {"completed", "completed_with_warnings"},
                    "run_dir": (manifest or {}).get("_run_dir"),
                }
            close_dir = Path(phases["close"]["run_dir"]) if phases["close"]["run_dir"] else None
            artifacts = {
                "daily_review": bool(close_dir and (close_dir / "daily_review_latest.md").exists()),
                "dashboard": bool(close_dir and (close_dir / "trading_dashboard_latest.html").exists()),
            }
            stock = _batch_status(
                con, "intraday_stock_flow_batch", trade_date, GOOD_STOCK_BATCH
            )
            sector = _batch_status(
                con, "intraday_sector_flow_batch", trade_date, GOOD_SECTOR_BATCH
            )
            tushare = _tushare_status(con, trade_date)
            ths = _ths_status(con, trade_date, minimum_ths_concepts)
            checks = {
                "calendar": bool(session["calendar_verified"]),
                "auction_run": phases["auction"]["passed"],
                "intraday_run": phases["intraday"]["passed"],
                "close_run": phases["close"]["passed"],
                "stock_flow": stock["passed"],
                "sector_flow": sector["passed"],
                "tushare_close": tushare["passed"],
                "ths_weekly": ths["passed"],
                "daily_review": artifacts["daily_review"],
                "dashboard": artifacts["dashboard"],
            }
            daily.append(
                {
                    **session,
                    "passed": all(checks.values()),
                    "checks": checks,
                    "phases": phases,
                    "stock_flow": stock,
                    "sector_flow": sector,
                    "tushare": tushare,
                    "ths": ths,
                    "artifacts": artifacts,
                }
            )
    finally:
        con.close()
    consecutive = 0
    for item in reversed(daily):
        if not item["passed"]:
            break
        consecutive += 1
    return {
        "as_of": as_of,
        "required_days": int(required_days),
        "observed_sessions": len(daily),
        "consecutive_passes": consecutive,
        "ready_for_p1": len(daily) >= int(required_days)
        and consecutive >= int(required_days),
        "daily": daily,
    }


def render_observation(result: dict[str, Any]) -> str:
    lines = [
        "# P0 Five-Trading-Day Observation",
        "",
        f"- As of: `{result['as_of']}`",
        f"- Required consecutive sessions: `{result['required_days']}`",
        f"- Consecutive strict passes: `{result['consecutive_passes']}`",
        f"- Ready to enter P1: `{str(result['ready_for_p1']).lower()}`",
        "",
        "| trade date | calendar | auction | intraday | close | stock flow | sector flow | TuShare close | THS weekly | review | dashboard | pass |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in result["daily"]:
        checks = item["checks"]
        mark = lambda value: "PASS" if value else "FAIL"
        lines.append(
            f"| {item['trade_date']} | {mark(checks['calendar'])} | "
            f"{mark(checks['auction_run'])} | {mark(checks['intraday_run'])} | "
            f"{mark(checks['close_run'])} | {mark(checks['stock_flow'])} | "
            f"{mark(checks['sector_flow'])} | {mark(checks['tushare_close'])} | "
            f"{mark(checks['ths_weekly'])} | {mark(checks['daily_review'])} | "
            f"{mark(checks['dashboard'])} | {mark(item['passed'])} |"
        )
    lines.extend(
        [
            "",
            "Strict rule: a degraded phase, unverified trading calendar, missing report, "
            "capital-flow coverage below 99.5%, incomplete TuShare close batch, or stale/incomplete "
            "THS snapshots without a source-recorded expected concept count reset the consecutive-pass count.",
            "",
            "```json",
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
        ]
    )
    return "\n".join(lines)
