from __future__ import annotations

import argparse
import json
from datetime import date, datetime
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trade_system.collection_profiles import resolve_phase, command_plan
from trade_system.source_authority import validate_production_plan

def _utf8_subprocess_env() -> dict[str, str]:
    """Return a deterministic text protocol for every Python child process.

    Windows scheduled tasks otherwise inherit a GBK console while the parent
    decodes captured output as UTF-8.  That produced U+FFFD in the parent and
    then crashed again when the replacement character was written to GBK.
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Schema is migrated once by the lock owner before child collectors are
    # launched.  Child processes must not replay bootstrap DDL against the
    # same DuckDB file.
    env["KPL_RUNTIME_SCHEMA_READY"] = "1"
    return env


def _decode_process_bytes(payload: bytes | str | None, stream_name: str) -> tuple[str, bool]:
    if payload is None:
        return "", False
    if isinstance(payload, str):
        return payload, False
    try:
        return payload.decode("utf-8", errors="strict"), False
    except UnicodeDecodeError as exc:
        # Preserve the exact offending bytes as ASCII escapes.  Never inject
        # U+FFFD into a Windows console or silently call corrupted text valid.
        safe = payload.decode("utf-8", errors="backslashreplace")
        detail = (
            f"\n[{stream_name}_encoding_error offset={exc.start} "
            f"bytes={payload[exc.start:exc.end].hex()} expected=utf-8]\n"
        )
        return safe + detail, True


def _safe_stream_write(stream, value: str) -> None:
    """Write diagnostics without allowing console encoding to abort a run."""
    if not value:
        return
    try:
        stream.write(value)
        stream.flush()
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe = value.encode(encoding, errors="backslashreplace")
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(safe)
            buffer.flush()
        else:  # pragma: no cover - StringIO and unusual embedded hosts
            stream.write(safe.decode(encoding, errors="strict"))
            stream.flush()



def main() -> int:
    parser = argparse.ArgumentParser(description="Transitional market collection adapter; no legacy decisions or user-page publication.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--phase", choices=("auto", "auction", "intraday", "close", "supplemental", "history"), default="auto")
    parser.add_argument("--history-start", default="")
    parser.add_argument("--history-end", default="")
    parser.add_argument("--history-max-days", type=int, default=0)
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--signal-limit", type=int, default=200, help="Bounded observation evidence count, not trading signals")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--step-timeout", type=int, default=900)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--migration-root", help="Verified disposable backup for offline/migration execution")
    parser.add_argument("--collector-contract", help="Explicit hash-bound transitional source/runtime/target manifest")
    parser.add_argument("--collector-contract-sha256")
    parser.add_argument("--collection-profile", choices=("priority",), default="priority")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if bool(args.collector_contract) != bool(args.collector_contract_sha256):
        parser.error("collector contract and approved hash must be supplied together")
    if args.migration_root and args.collector_contract:
        parser.error("migration copy and task handover are separate modes")
    selected_phase = resolve_phase(args.phase)
    backend, python = ROOT, sys.executable
    if args.collector_contract:
        from trade_system.migration_boundary import verify_collection_contract
        contract = verify_collection_contract(args.collector_contract, args.collector_contract_sha256,
                                               args.db, args.reports_dir)
        backend, python = Path(contract["source_root"]), contract["python"]
        if selected_phase not in ("auction", "intraday", "close", "supplemental"):
            parser.error("scheduled handover does not authorize historical backfills")
    elif not args.dry_run:
        if not args.migration_root:
            parser.error("verified disposable copy or explicit collection handover contract required; old business execution retired")
        from trade_system.migration_boundary import require_copy
        require_copy(args.migration_root, args.db, args.reports_dir)
    if not args.skip_collect and args.trade_date != date.today().isoformat() and selected_phase != "history":
        parser.error("current source snapshots cannot be relabelled as historical dates")
    if args.step_timeout <= 0 or not 1 <= args.signal_limit <= 1000:
        parser.error("positive bounded timeout and observation count required")
    run_id = args.run_id or f"{args.trade_date.replace('-', '')}_{datetime.now().strftime('%H%M%S')}_{os.getpid()}"
    # Run id becomes a directory component, never an operator-supplied path.
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
        parser.error("safe run id required")
    artifact_dir = Path(args.reports_dir).resolve() / "runs" / run_id
    plan_as_of = args.as_of or datetime.now().isoformat()
    plan = command_plan(args.db, args.trade_date, include_collection=not args.skip_collect,
        signal_limit=args.signal_limit, reports_dir=str(artifact_dir), phase=selected_phase,
        as_of_time=args.as_of or None, collection_profile=args.collection_profile,
        history_start=args.history_start, history_end=args.history_end, history_max_days=args.history_max_days)
    plan = [(name, [python, *command[1:]], optional) for name, command, optional in plan]
    if not args.skip_collect:
        validate_production_plan(selected_phase, [name for name, _, _ in plan])
    if args.dry_run:
        for name, command, _ in plan:
            print(f"RUN {name}: {' '.join(command)}")
        return 0
    # A dedicated collector interpreter must be healthy before acquiring any
    # database lock. No fallback to the current shell/global interpreter.
    if args.collector_contract:
        check = subprocess.run([python, "-I", "-B", "-m", "pip", "check"], capture_output=True, timeout=60)
        if check.returncode:
            dependency_log=artifact_dir/'dependency-check.log'
            dependency_log.write_text((check.stdout+check.stderr).decode('utf-8','backslashreplace'),encoding='utf-8')
            raise ValueError(f"collector dependency check failed ({check.returncode}); details: {dependency_log}; no collection performed")
    from trade_system.pipeline_runtime import PipelineLock, PipelineAlreadyRunning, RunManifest
    from trade_system.trading_calendar import trading_session_status, ensure_trading_session_status
    manifest = RunManifest(args.reports_dir, run_id, args.trade_date, selected_phase)
    manifest.data.update(scope="transitional_market_collection_only", user_pages_published=False,
                         collector_contract_sha256=args.collector_contract_sha256, execution_ready=False)
    manifest.write()
    try:
        with PipelineLock(args.db, run_id):
            session = trading_session_status(args.db, args.trade_date)
            if selected_phase != "history" and not args.skip_collect and session.state == "unverified":
                # The parent already holds the single-writer guard. Fetch only
                # this missing session; never infer it from weekday or prices.
                session = ensure_trading_session_status(args.db, args.trade_date)
            if selected_phase != "history" and not args.skip_collect and session.state != "open":
                state = "skipped_market_closed" if session.state == "closed" else "blocked_calendar_unverified"
                manifest.finish(state, session.reason)
                print("MARKET_CLOSED" if session.state == "closed" else "MARKET_CALENDAR_UNVERIFIED")
                return 0 if session.state == "closed" else 2
            # Existing collector code owns schema setup. The new adapter never
            # weakens legacy_connect's primary/V2 write barrier.
            bootstrap = ("import sys; from trade_system.schema import init_schema; "
                         "from trade_system.data_store import connect_duckdb; c=connect_duckdb(sys.argv[1]); "
                         "init_schema(c); c.close()")
            initialization = subprocess.run([python, "-X", "utf8", "-c", bootstrap, str(args.db)],
                cwd=backend, capture_output=True, timeout=args.step_timeout, env=_utf8_subprocess_env())
            if initialization.returncode:
                raise ValueError("collector schema initialization failed: "+initialization.stderr.decode("utf-8", "backslashreplace")[-500:])
            failed, warnings, pending = [], [], []
            from trade_system.collection_profiles import phase_tasks
            required = {task.name: task.required for task in phase_tasks(selected_phase)}
            for name, command, _ in plan:
                from trade_system.collection_profiles import task_due
                due, reason = task_due(args.db, args.trade_date, name, phase=selected_phase,
                                       now=datetime.fromisoformat(plan_as_of))
                if not due:
                    cooling_down = reason.startswith('retry cooldown')
                    awaiting = reason.startswith('publication pending:')
                    state = ('degraded' if required[name] else 'warning') if cooling_down else 'awaiting_publication' if awaiting else 'skipped'
                    manifest.add_step(name, state, command, reason=reason, required=required[name])
                    if cooling_down:
                        (failed if required[name] else warnings).append(name)
                    if awaiting:
                        pending.append(name)
                    continue
                started = datetime.now()
                log = artifact_dir / (name+".log")
                manifest.upsert_step(name, "running", command, started_at=started.isoformat(), log_path=str(log))
                try:
                    context={'demand_id':name, 'consumer':'market_review_and_observation',
                        'phase':selected_phase,'session':args.trade_date,
                        'refresh_reason':'supplemental_retry' if selected_phase=='supplemental' else 'profile_due',
                        'coverage_before':reason}
                    env=_utf8_subprocess_env()
                    env['STOCKDATA_REQUEST_CONTEXT']=json.dumps(context,sort_keys=True)
                    process = subprocess.run(command, cwd=backend, capture_output=True,
                        env=env, timeout=args.step_timeout)
                    out, bad_out = _decode_process_bytes(process.stdout, "stdout")
                    err, bad_err = _decode_process_bytes(process.stderr, "stderr")
                    code = process.returncode or (-2 if bad_out or bad_err else 0)
                except subprocess.TimeoutExpired as exc:
                    out, _ = _decode_process_bytes(exc.stdout, "stdout")
                    err, _ = _decode_process_bytes(exc.stderr, "stderr")
                    code = -1
                    err += "\nBounded collector timeout"
                log.write_text("[stdout]\n"+out+"\n[stderr]\n"+err, encoding="utf-8")
                disclosure_pending = code == 4 and name in {'collect_xiaodefa', 'collect_xiaodefa_critical'}
                status = "awaiting_publication" if disclosure_pending else "completed" if code == 0 else "degraded" if required[name] else "warning"
                manifest.upsert_step(name, status, command, return_code=code, log_path=str(log),
                                     required=required[name],
                                     request_context=context,
                                     duration_seconds=round((datetime.now()-started).total_seconds(), 3))
                if code:
                    (pending if disclosure_pending and required[name] else failed if required[name] else warnings).append(name)
                # Independent data sources still run after one provider fails.
                # No failed run is reported as a successful publication.
            if selected_phase in ('close', 'supplemental'):
                from trade_system.collection_profiles import publication_readiness
                manifest.data['publication_readiness'] = publication_readiness(args.db, args.trade_date)
            if selected_phase == 'supplemental' and not failed and not pending:
                from trade_system.pipeline_runtime import latest_manifests
                prior = latest_manifests(args.reports_dir).get((args.trade_date, 'close'))
                if (prior and prior.get('scope') == manifest.data['scope']
                        and str(prior.get('collector_contract_sha256', '')).lower()
                        == str(args.collector_contract_sha256).lower()):
                    manifest.data['recovery_of'] = {'phase': 'close', 'run_id': prior['run_id'],
                        'status': prior['status'], 'completed_at': prior.get('completed_at'),
                        'manifest_sha256': prior['_manifest_sha256'],
                        'scope': 'same_day_close_recovery_not_auction_or_intraday_replay'}
            manifest.data['pending'] = pending
            manifest.finish("completed_with_degradation" if failed else "awaiting_publication" if pending else "completed_with_warnings" if warnings else "completed",
                            ",".join(failed) or None, warnings=warnings)
            print(f"COLLECTION_COMPLETE date={args.trade_date} failed={len(failed)} user_pages_published=false manifest={manifest.path}")
            return 2 if failed else 0
    except PipelineAlreadyRunning as exc:
        manifest.finish("blocked", str(exc))
        return 3
    except Exception as exc:
        manifest.finish("failed", str(exc))
        _safe_stream_write(sys.stderr, str(exc)+"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
