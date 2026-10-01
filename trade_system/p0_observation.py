"""Strict rolling acceptance for the five-session P0 observation window."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from trade_system.quality import table_exists
from trade_system.ths_quality import canonical_ths_snapshot
from trade_system.trading_calendar import open_session_dates
from trade_system.pipeline_runtime import all_manifests, load_observation_contract, observation_windows


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


def phase_evidence_errors(manifest, trade_date):
    """A green summary cannot override missed windows or failed required work."""
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    if not manifest:return ['missing_run']
    errors=[]
    try:
        values=[]
        for key in ('started_at','completed_at'):
            value=datetime.fromisoformat(manifest[key])
            values.append(value.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if value.tzinfo is None
                          else value.astimezone(ZoneInfo('Asia/Shanghai')))
        started,ended=values
        if started>ended or started.date().isoformat()!=trade_date or ended.date().isoformat()!=trade_date:
            errors.append('invalid_or_cross_day_phase_time')
        phase=manifest['phase']
        if phase=='auction' and not time(9,15)<=started.time()<=ended.time()<=time(9,30):errors.append('auction_outside_window')
        if phase=='intraday' and not time(9,30)<=started.time()<=ended.time()<=time(15,5):errors.append('intraday_outside_window')
        if phase=='intraday' and (time(11,30)<=started.time()<time(13)
                                 or started.time()<time(11,30)<ended.time()):errors.append('intraday_crosses_lunch')
        if phase in ('close','supplemental') and started.time()<time(15,5):errors.append('close_before_window')
    except (KeyError,ValueError,TypeError):
        errors.append('phase_time_unverified')
    steps=manifest.get('steps') or []
    if not isinstance(steps,list) or not steps:
        return errors+['step_evidence_missing']
    for step in steps:
        if not isinstance(step,dict):
            errors.append('step_evidence_invalid')
            continue
        if step.get('required',True) and step.get('status') not in {'completed','skipped'}:
            errors.append('required_step_not_complete:'+str(step.get('name')))
        if step.get('required',True) and step.get('status')=='skipped' and not step.get('reason'):
            errors.append('required_skip_without_evidence:'+str(step.get('name')))
    return errors


def _local_stamp(value: Any) -> datetime:
    stamp = datetime.fromisoformat(value)
    return (stamp.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if stamp.tzinfo is None
            else stamp.astimezone(ZoneInfo('Asia/Shanghai')))


def _receipt_errors(manifest: dict, trade_date: str, contract_sha256: str) -> list[str]:
    errors = phase_evidence_errors(manifest, trade_date)
    if manifest.get('status') not in {'completed', 'completed_with_warnings'}:
        errors.append('phase_not_qualified')
    if manifest.get('scope') != 'transitional_market_collection_only':
        errors.append('phase_scope_unqualified')
    if str(manifest.get('collector_contract_sha256', '')).lower() != contract_sha256.lower():
        errors.append('collector_contract_mismatch')
    return errors


def _receipt_order(manifest: dict) -> tuple:
    try:
        started = _local_stamp(manifest.get('started_at')).timestamp()
    except (ValueError, TypeError, OverflowError):
        started = float('-inf')
    try:
        ended = _local_stamp(manifest.get('completed_at')).timestamp()
    except (ValueError, TypeError, OverflowError):
        ended = float('inf')
    return (started, ended, manifest.get('status') not in {'completed', 'completed_with_warnings'},
            str(manifest.get('run_id') or ''))


def _publication_after(publication: dict, completed_at: Any, trade_date: str) -> bool:
    try:
        completed = _local_stamp(completed_at)
        published = datetime.fromisoformat(publication['published_at'])
        if published.tzinfo is None:
            return False
        published = published.astimezone(ZoneInfo('Asia/Shanghai'))
        as_of = _local_stamp(publication['as_of'])
        return bool(publication.get('passed') and published.date().isoformat() == trade_date
                    and published >= completed and as_of >= completed)
    except (KeyError, ValueError, TypeError):
        return False


def audit_phase_windows(manifests: list[dict], trade_date: str, phase: str,
                        collector_contract_sha256: str, policy: dict | None,
                        publication: dict | None = None, *, contract_error: str | None = None) -> dict:
    """Qualify all scheduled slots, retaining failures and bounded recoveries.

    A late start can recover only a timely attempt with the same collector hash
    inside its original deadline. Supplemental recovery is solely for a linked
    failed close, followed by an independently verified same-day publication.
    """
    receipts = [item for item in manifests if str(item.get('trade_date', ''))[:10] == trade_date
                and str(item.get('phase', '')).lower() == phase]
    receipts.sort(key=_receipt_order)
    selected = receipts[-1] if receipts else {}
    original = selected
    recovered_by_supplemental = False
    if contract_error:
        windows = []
    else:
        try:
            windows = observation_windows(policy, trade_date, phase)
        except (ValueError, TypeError, KeyError) as exc:
            windows = []
            contract_error = str(exc)
    assigned = set()
    evidence = []
    for window in windows:
        start, start_latest, deadline = (_local_stamp(window[name]) for name in
                                        ('start_at', 'start_latest_at', 'deadline_at'))
        attempts = []
        timely_same_contract = False
        last_manifest = None
        for index, manifest in enumerate(receipts):
            try:
                started = _local_stamp(manifest['started_at'])
            except (KeyError, ValueError, TypeError):
                continue
            if not start <= started < deadline:
                continue
            assigned.add(index)
            errors = _receipt_errors(manifest, trade_date, collector_contract_sha256)
            same_contract = str(manifest.get('collector_contract_sha256', '')).lower() == collector_contract_sha256.lower()
            scope_ok = manifest.get('scope') == 'transitional_market_collection_only'
            if started > start_latest and not timely_same_contract:
                errors.append('late_start_without_same_window_attempt')
            if started <= start_latest and same_contract and scope_ok:
                timely_same_contract = True
            try:
                ended = _local_stamp(manifest['completed_at'])
                if ended > deadline:
                    errors.append('completion_exceeds_original_window_budget')
                if manifest.get('deadline_epoch') is not None:
                    run_deadline = float(manifest['deadline_epoch'])
                    if not start.timestamp() <= run_deadline <= deadline.timestamp():
                        errors.append('request_deadline_exceeds_original_window_budget')
            except (KeyError, ValueError, TypeError, OverflowError):
                if 'phase_time_unverified' not in errors:
                    errors.append('phase_time_unverified')
            if manifest.get('observation_window_id') not in (None, window['window_id']):
                errors.append('observation_window_identity_mismatch')
            attempts.append({'run_id': manifest.get('run_id'), 'status': manifest.get('status'),
                             'started_at': manifest.get('started_at'),
                             'completed_at': manifest.get('completed_at'),
                             'manifest_sha256': manifest.get('_manifest_sha256'),
                             'passed': not errors, 'evidence_errors': errors})
            last_manifest = manifest
        passed = bool(attempts and attempts[-1]['passed'])
        recovery = None
        # Missing or late close starts cannot be fabricated by a later replay.
        if (phase == 'close' and not passed and last_manifest and timely_same_contract
                and str(last_manifest.get('collector_contract_sha256', '')).lower() == collector_contract_sha256.lower()
                and last_manifest.get('scope') == 'transitional_market_collection_only'):
            for candidate in sorted(manifests, key=_receipt_order):
                bound = candidate.get('recovery_of') or {}
                if (str(candidate.get('trade_date', ''))[:10] != trade_date
                        or candidate.get('phase') != 'supplemental'
                        or not isinstance(bound, dict)
                        or bound.get('phase') != 'close'
                        or bound.get('run_id') != last_manifest.get('run_id')
                        or bound.get('completed_at') != last_manifest.get('completed_at')
                        or bound.get('manifest_sha256') != last_manifest.get('_manifest_sha256')
                        or bound.get('scope') != 'same_day_close_recovery_not_auction_or_intraday_replay'
                        or _receipt_errors(candidate, trade_date, collector_contract_sha256)):
                    continue
                try:
                    began = _local_stamp(candidate['started_at'])
                    ended = _local_stamp(candidate['completed_at'])
                    if (began <= _local_stamp(last_manifest['completed_at'])
                            or (ended - began).total_seconds() > 3600
                            or not _publication_after(publication or {}, candidate['completed_at'], trade_date)):
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                recovery = candidate
            if recovery:
                original, selected = last_manifest, recovery
                recovered_by_supplemental = True
                passed = True
        elif last_manifest:
            selected = last_manifest
        evidence.append({**window, 'passed': passed, 'attempts': attempts,
                         'recovered_in_window': bool(passed and not recovery and len(attempts) > 1
                                                     and any(not item['passed'] for item in attempts[:-1])),
                         'supplemental_run_id': (recovery or {}).get('run_id')})
    errors = (["observation_contract_unverified:" + contract_error] if contract_error else [])
    errors += ['required_window_not_qualified:' + row['window_id'] for row in evidence if not row['passed']]
    if any(index not in assigned and item.get('scope') == 'transitional_market_collection_only'
           and str(item.get('collector_contract_sha256', '')).lower() == collector_contract_sha256.lower()
           and 'phase_time_unverified' in phase_evidence_errors(item, trade_date)
           for index, item in enumerate(receipts)):
        errors.append('unassigned_phase_time_unverified')
    if not evidence and not errors:
        errors.append('required_observation_windows_missing')
    return {
        'run_id': selected.get('run_id'), 'status': selected.get('status', 'missing'),
        'passed': bool(evidence and all(row['passed'] for row in evidence) and not errors),
        'evidence_errors': errors, 'run_dir': selected.get('_run_dir'),
        'completed_at': selected.get('completed_at'),
        'original_run_id': original.get('run_id'), 'original_status': original.get('status'),
        'recovered_by_supplemental': recovered_by_supplemental,
        'required_window_count': len(evidence), 'qualified_window_count': sum(row['passed'] for row in evidence),
        'windows': evidence,
        'unassigned_attempts': [{'run_id': item.get('run_id'), 'status': item.get('status'),
                                 'started_at': item.get('started_at'),
                                 'evidence_errors': _receipt_errors(item, trade_date, collector_contract_sha256)
                                 + ['outside_required_observation_window']}
                                for index, item in enumerate(receipts) if index not in assigned],
    }


def _observation_evidence(workspace, pointer, day):
    """Read the original observed bundle, not its expired evening presentation."""
    from datetime import time
    from trade_system.v2.publisher import read_bundle
    from trade_system.v2.domain import identity
    from trade_system.v2.observation_capture import read_sampling
    from trade_system.v2.observation_workspace import local_clock, TTL_SECONDS
    try:
        _,files=read_bundle(Path(workspace)/'observation-publication',pointer or {})
        value=json.loads(files['observation.json'])
        if value['snapshot_id']!=identity({k:v for k,v in value.items() if k!='snapshot_id'}):
            raise ValueError('observation changed')
        at=local_clock(value['as_of'])
        if at.date().isoformat()!=day or not (time(9,30)<=at.time()<=time(11,30) or time(13)<=at.time()<=time(15)):
            raise ValueError('observation outside live session')
        sampling_id=value['sampling']['sampling_id']
        if len(sampling_id)!=64 or any(c not in '0123456789abcdef' for c in sampling_id):
            raise ValueError('sampling identity invalid')
        sampling=read_sampling(Path(workspace)/'sampling'/sampling_id,day)
        if not sampling['codes']:raise ValueError('empty sampling scope')
        rows={r['instrument']:r for r in value['rows']}
        for code in sampling['codes']:
            row=rows[code]
            from datetime import datetime
            event=datetime.fromisoformat(row['source_event_time'])
            received=datetime.fromisoformat(row['received_at'])
            if (row['state']!='current_observation_not_executable' or not row['price']>0
                    or not event<=received<=at or not 0<=(at-event).total_seconds()<=TTL_SECONDS):
                raise ValueError('subject not qualified at observation time')
        return {'passed':True,'sampling_id':sampling_id,'snapshot_id':value['snapshot_id'],
            'subjects':len(sampling['codes']),'as_of':value['as_of'],'pointer':pointer}
    except (KeyError,ValueError,TypeError,OSError):
        return {'passed':False,'error':'pre_session_scope_or_live_observation_unverified'}



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
                   'published_at': manifest.get('published_at'), 'content_contract': contract,
                   'observation':_observation_evidence(workspace,data.get('observation_evidence'),day)}
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
    collector_contract_path: str | Path | None = None,
) -> dict[str, Any]:
    if len(collector_contract_sha256) != 64 or any(c not in '0123456789abcdef' for c in collector_contract_sha256.lower()):
        raise ValueError('explicit collector contract SHA256 required')
    manifests = all_manifests(reports_dir)
    policy, contract_error = None, None
    try:
        if collector_contract_path is None:
            raise ValueError('original accepted collector contract path required')
        policy = load_observation_contract(collector_contract_path, collector_contract_sha256)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        contract_error = str(exc)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        sessions = _sessions(con, as_of, max(1, int(required_days)))
        publications, publication_error = _publications(workspace, {s['trade_date'] for s in sessions})
        daily = []
        for session in sessions:
            trade_date = session["trade_date"]
            phases = {}
            publication = publications.get(trade_date, {'passed': False, 'error': publication_error or 'missing_same_day_publication'})
            for phase in PHASES:
                phases[phase] = audit_phase_windows(manifests, trade_date, phase,
                    collector_contract_sha256, policy, publication, contract_error=contract_error)
            if publication['passed']:
                try:
                    completed = _local_stamp(phases['close']['completed_at'])
                    if _local_stamp(publication['as_of']) < completed:
                        publication = dict(publication, passed=False, error='publication_precedes_close_completion')
                    elif not _publication_after(publication, phases['close']['completed_at'], trade_date):
                        publication = dict(publication, passed=False, error='independent_same_day_publication_unverified')
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
                "pre_session_observation": publication.get('observation',{}).get('passed',False),
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
        "collector_contract_path": str(Path(collector_contract_path).resolve()) if collector_contract_path else None,
        "observation_window_contract": policy,
        "observation_window_contract_error": contract_error,
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
            "Strict rule: every required observation window in the original accepted collector contract must qualify. "
            "Pre-session preparation, out-of-window successes and later green summaries cannot replace missing slots. "
            "A degraded phase, unverified trading calendar, missing report, "
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
