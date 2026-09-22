"""Strict rolling acceptance for the five-session P0 observation window."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_exists
from trade_system.ths_quality import canonical_ths_snapshot
from trade_system.trading_calendar import open_session_dates
from trade_system.pipeline_runtime import latest_manifests


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



def _publications(workspace, dates):
    """Verify retained daily bundles, including the current published pointer."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from trade_system.v2.domain import identity, canonical
    from trade_system.v2.publisher import read_current, read_bundle
    from trade_system.v2.research_product_view import render, export_projection
    root = Path(workspace) / 'publication'
    result = {}
    try:
        current, current_files = read_current(root)
        pointers = dict(current.get('published_days', {}))
        if len(pointers) > 64:
            raise ValueError('published day index exceeds audit budget')
        current_day = (json.loads(current_files.get('desk.json', b'{}')).get('market') or {}).get('trade_date')
        if current_day:
            pointers[current_day] = {'run_id':current['run_id'],'generation':current['generation'],
                'manifest_sha256':hashlib.sha256(canonical(current).encode()).hexdigest()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, 'current_publication_' + type(exc).__name__
    for day in dates:
        if day not in pointers:
            continue
        try:
            pointer = pointers[day]
            manifest, files = read_bundle(root, pointer)
            if set(manifest['artifacts']) != {'index.html', 'desk.json'}:
                raise ValueError('publication identity or members differ')
            if manifest['generation'] > current['generation']:
                raise ValueError('historical generation exceeds current')
            data = json.loads(files['desk.json'])
            market = data.get('market') or {}
            marker = '<script type="application/json" id="data">'
            head, body = files['index.html'].decode('utf-8').split(marker)
            payload, tail = body.split('</script>', 1)
            contract = manifest.get('content_contract')
            if contract:
                if (contract.get('name') != 'inline_research_desk' or contract.get('version') != 1
                        or hashlib.sha256(canonical(json.loads(payload)).encode()).hexdigest() != contract.get('projection_sha256')):
                    raise ValueError('unsupported or changed original publication contract')
            else:
                # Legacy bundles have no versioned contract. Only a current,
                # byte-compatible renderer can verify them; do not invent proof.
                expected_head, expected_body = render(data).split(marker)
                _, expected_tail = expected_body.split('</script>', 1)
                if head != expected_head or tail != expected_tail or json.loads(payload) != export_projection(data):
                    raise ValueError('legacy publication contract unavailable')
            if (data.get('report_id') != identity({k:v for k,v in data.items() if k != 'report_id'})
                    or market.get('snapshot_id') != identity({k:v for k,v in market.items() if k != 'snapshot_id'})
                    or data.get('execution_ready') is not False
                    or market.get('scope') != 'read_only_market_review_not_execution'
                    or market.get('execution_ready') is not False
                    or market.get('trade_date') != day or not market.get('stocks')):
                raise ValueError('publication content contract differs')
            at = datetime.fromisoformat(market['as_of'])
            if at.tzinfo is None:
                raise ValueError('publication timestamp must be aware')
            at = at.astimezone(ZoneInfo('Asia/Shanghai'))
            row = {'passed': at.date().isoformat() == day and at.hour >= 16,
                   'as_of': at.isoformat(), 'generation': manifest['generation'],
                   'run_id': manifest['run_id'], 'manifest_sha256': pointer['manifest_sha256'],
                   'published_at': manifest.get('published_at'), 'content_contract': contract}
            result[day] = row
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result[day] = {'passed': False, 'error': 'publication_' + type(exc).__name__}
    return result, None



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
                flags = con.execute("SELECT exchange,is_open FROM tushare_trade_cal WHERE cal_date=? "
                                    "AND exchange IN ('SSE','SZSE') ORDER BY exchange", [cursor]).fetchall()
                sessions.append({"trade_date": cursor.isoformat(), "calendar_verified": flags == [('SSE',1),('SZSE',1)]})
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
    workspace: str | Path,
    collector_contract_sha256: str,
) -> dict[str, Any]:
    if len(collector_contract_sha256) != 64 or any(c not in '0123456789abcdef' for c in collector_contract_sha256.lower()):
        raise ValueError('explicit collector contract SHA256 required')
    manifests = latest_manifests(reports_dir)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        sessions = _sessions(con, as_of, max(1, int(required_days)))
        publications, publication_error = _publications(workspace, {s['trade_date'] for s in sessions})
        daily = []
        for session in sessions:
            trade_date = session["trade_date"]
            phases = {}
            for phase in PHASES:
                manifest = manifests.get((trade_date, phase))
                original = manifest
                if phase == 'close' and manifest:
                    recovery = manifests.get((trade_date, 'supplemental')) or {}
                    bound = recovery.get('recovery_of') or {}
                    if (recovery.get('status') in {'completed', 'completed_with_warnings'}
                            and recovery.get('scope') == 'transitional_market_collection_only'
                            and str(recovery.get('collector_contract_sha256', '')).lower() == collector_contract_sha256.lower()
                            and str(manifest.get('collector_contract_sha256', '')).lower() == collector_contract_sha256.lower()
                            and bound.get('phase') == 'close' and bound.get('run_id') == manifest.get('run_id')
                            and bound.get('completed_at') == manifest.get('completed_at')
                            and bound.get('manifest_sha256') == manifest['_manifest_sha256']
                            and bound.get('scope') == 'same_day_close_recovery_not_auction_or_intraday_replay'
                            and str(recovery.get('started_at', '')) > str(manifest.get('completed_at') or '9999')):
                        manifest = recovery
                status = str((manifest or {}).get("status") or "missing")
                phases[phase] = {
                    "run_id": (manifest or {}).get("run_id"),
                    "status": status,
                    # Optional post-close capabilities (currently northbound
                    # historical net-buy) may be unavailable without invalidating
                    # the day's core close run.  Only the explicit warning status
                    # is accepted here; degraded/blocked/chain-failure runs stay
                    # fail-closed.
                    "passed": status in {"completed", "completed_with_warnings"}
                    and (manifest or {}).get('scope') == 'transitional_market_collection_only'
                    and str((manifest or {}).get('collector_contract_sha256', '')).lower() == collector_contract_sha256.lower(),
                    "run_dir": (manifest or {}).get("_run_dir"),
                    "completed_at": (manifest or {}).get('completed_at'),
                    "original_run_id": (original or {}).get('run_id'),
                    "original_status": (original or {}).get('status'),
                    "recovered_by_supplemental": bool(original and manifest is not original),
                }
            publication = publications.get(trade_date, {'passed': False, 'error': publication_error or 'missing_same_day_publication'})
            if publication['passed']:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                try:
                    completed = datetime.fromisoformat(phases['close']['completed_at'])
                    if completed.tzinfo is None:
                        completed = completed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                    if datetime.fromisoformat(publication['as_of']) < completed:
                        publication = dict(publication, passed=False, error='publication_precedes_close_completion')
                except (KeyError, ValueError, TypeError):
                    publication = dict(publication, passed=False, error='close_completion_unverified')
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
                "publication": publication['passed'],
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
                    "publication": publication,
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
        "collector_contract_sha256": collector_contract_sha256.lower(),
        "scope": "market_collection_and_publication_not_account_performance_or_trading_permission",
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
        "| trade date | calendar | auction | intraday | close | stock flow | sector flow | TuShare close | THS weekly | publication | pass |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in result["daily"]:
        checks = item["checks"]
        mark = lambda value: "PASS" if value else "FAIL"
        lines.append(
            f"| {item['trade_date']} | {mark(checks['calendar'])} | "
            f"{mark(checks['auction_run'])} | {mark(checks['intraday_run'])} | "
            f"{mark(checks['close_run'])} | {mark(checks['stock_flow'])} | "
            f"{mark(checks['sector_flow'])} | {mark(checks['tushare_close'])} | "
            f"{mark(checks['ths_weekly'])} | {mark(checks['publication'])} | {mark(item['passed'])} |"
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
