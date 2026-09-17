from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trade_system.collection_profiles import resolve_phase
from trade_system.config import default_trade_date
from trade_system.source_authority import validate_production_plan

CommandStep = tuple[str, list[str], bool]

# P0#2: how many calendar days back the close-phase TuShare sync scans for gaps.
# Already-synced dates are skipped via the history checkpoint, so this only fetches
# missing/failed recent sessions (e.g. a prior day the relay dropped).
TUSHARE_GAPFILL_LOOKBACK_DAYS = 10

# Audit P2 #1: close readiness / capital-flow acceptance window.  Tightened from
# 21600s (6h) to 7200s (2h) so a degraded close collector that leaves a stale
# intraday snapshot (last intraday fetch ~15:00, >2h before the 17:30 close run) is
# fail-closed (close_blocked) instead of being accepted as close-ready.  Freshly
# re-collected close data (<1h old) still passes.
CLOSE_READINESS_MAX_AGE_SECONDS = 7200


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


def command_plan(
    db_path: str,
    trade_date: str | None = None,
    *,
    include_collection: bool = False,
    signal_limit: int = 200,
    reports_dir: str = "reports",
    as_of_time: str | None = None,
    collection_profile: str = "priority",
    phase: str | None = None,
    history_start: str = "",
    history_end: str = "",
    history_max_days: int = 0,
    include_research: bool | None = None,
) -> list[CommandStep]:
    py = sys.executable
    selected_date = trade_date or default_trade_date(db_path)
    if collection_profile != "priority":
        raise ValueError("the legacy collection profile was retired; use priority")
    if phase == "full":
        raise ValueError("the legacy full phase was retired; use close, history, or a narrow phase")
    # Direct callers that request collection without a phase now use the same
    # canonical close plan as the scheduler.  This removes the old implicit
    # compatibility fan-out from the reachable default path.
    if include_collection and phase is None:
        phase = "close"
    if include_research:
        raise ValueError("automatic legacy research retired; use the independent research product")
    report = lambda name: str(Path(reports_dir) / name)
    steps: list[CommandStep] = []
    if include_collection and phase is not None:
        # Phase mode is intentionally narrow and is the only collection path.
        if phase == "auction":
            collection_steps = [
                ("collect_market_context", [py, "fetch_all.py", "--db", db_path, "--date", selected_date, "--only-market"], False),
                ("check_kpl_connectivity", [py, "scripts/check_kpl_connectivity.py", "--db", db_path, "--date", selected_date, "--out", report("kpl_connectivity_latest.md")], False),
                ("collect_realtime_limit_pool", [py, "scripts/collect_realtime_limit_pool.py", "--db", db_path, "--date", selected_date, "--out", report("realtime_candidate_pool_latest.md")], False),
                ("collect_auction_evidence", [py, "scripts/collect_auction_evidence.py", "--db", db_path, "--date", selected_date, "--max-stocks", str(signal_limit), "--out", report("auction_collection_latest.json")], False),
                ("build_auction_evidence", [py, "scripts/build_auction_evidence.py", "--db", db_path, "--trade-date", selected_date, "--out", report("auction_evidence_latest.md")], False),
            ]
        elif phase == "intraday":
            collection_steps = [
                ("collect_market_context", [py, "fetch_all.py", "--db", db_path, "--date", selected_date, "--only-market"], False),
                ("check_kpl_connectivity", [py, "scripts/check_kpl_connectivity.py", "--db", db_path, "--date", selected_date, "--out", report("kpl_connectivity_latest.md")], False),
                ("collect_realtime_limit_pool", [py, "scripts/collect_realtime_limit_pool.py", "--db", db_path, "--date", selected_date, "--out", report("realtime_candidate_pool_latest.md")], False),
                ("collect_intraday_stock_flow_market", [py, "scripts/collect_intraday_stock_flow_market.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_stock_flow_latest.md")], False),
                # Phase mode never runs full L2; keep candidate stock curves fresh.
                ("collect_l2_focus", [py, "scripts/collect_l2_focus.py", "--db", db_path, "--date", selected_date,
                 "--max-stocks", str(min(60, max(20, signal_limit // 3))),
                 "--total-budget-seconds", "90", "--auto-boost-if-kpl-stale",
                 "--out", report("l2_focus_collection_latest.md")], False),
                # Refresh the sector snapshot after the bounded L2 work so its
                # 10-minute readiness TTL cannot expire while L2 is running.
                ("collect_intraday_sector_flow_full", [py, "scripts/collect_intraday_sector_flow_full.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_sector_flow_latest.md")], False),
                ("derive_market_context", [py, "scripts/derive_market_context.py", "--db", db_path, "--date", selected_date, "--out", report("market_context_latest.json")], False),
            ]
        elif phase == "close":
            collection_steps = [
                ("collect_market_context", [py, "fetch_all.py", "--db", db_path, "--date", selected_date, "--only-market"], False),
                ("check_kpl_connectivity", [py, "scripts/check_kpl_connectivity.py", "--db", db_path, "--date", selected_date, "--out", report("kpl_connectivity_latest.md")], False),
                # P0#2: close guarantees the newest snapshot here; the following
                # physical-kline step also repairs the bounded recent-gap window
                # before copying it, so older history remains a separate batch.
                ("sync_tushare_close", [py, "scripts/backfill_2026_tushare.py", "--db", db_path,
                 "--start-date", (date.fromisoformat(selected_date) - timedelta(days=TUSHARE_GAPFILL_LOOKBACK_DAYS)).strftime("%Y%m%d"),
                 "--end-date", selected_date.replace("-", ""),
                 "--datasets", "daily,daily_basic,adj_factor,moneyflow,industry_flow", "--gap-only", "--max-days", "1",
                 "--retry-passes", "1", "--retry-delay-seconds", "2.0",
                 "--report", report("tushare_close_latest.md")], False),
                # Push TuShare daily into physical kline so data_chain / collectors
                # that still read ``kline`` see the same session as v_kline_daily.
                ("sync_tushare_ohlc_core", [py, "scripts/sync_tushare_ohlc.py", "--db", db_path,
                 "--start-date", (date.fromisoformat(selected_date) - timedelta(days=TUSHARE_GAPFILL_LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
                 "--end-date", selected_date, "--repair-close-gaps", "--repair-budget-seconds", "180"], False),
                # The official same-day THS snapshot must exist before the
                # stock-flow aggregate is grouped into concepts.  Running this
                # after sector flow created same-date rows based on a prior
                # membership snapshot and mixed two data versions on the page.
                ("collect_ths_concepts_api", [py, "scripts/collect_ths_concepts_api.py", "--db", db_path,
                 "--snapshot-date", selected_date], False),
                ("collect_hithink_limit_pool_daily", [py, "scripts/collect_hithink_limit_pool_daily.py", "--db", db_path,
                 "--date", selected_date, "--out", report("hithink_limit_pool_latest.json"),
                 "--assume-pipeline-lock"], False),
                ("collect_realtime_limit_pool", [py, "scripts/collect_realtime_limit_pool.py", "--db", db_path, "--date", selected_date, "--out", report("realtime_candidate_pool_latest.md")], False),
                ("collect_kpl_stock_flow_focus", [py, "scripts/collect_capital_flow_focus.py", "--db", db_path,
                 "--date", selected_date, "--max-stocks", str(signal_limit), "--max-sectors", "0",
                 "--moneyflow-only", "--no-resilient-fallback", "--total-budget-seconds", "45",
                 "--out", report("kpl_stock_flow_focus_latest.md")], False),
                ("collect_intraday_stock_flow_market", [py, "scripts/collect_intraday_stock_flow_market.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_stock_flow_latest.md")], False),
                ("collect_intraday_sector_flow_full", [py, "scripts/collect_intraday_sector_flow_full.py", "--db", db_path, "--date", selected_date, "--out", report("intraday_sector_flow_latest.md")], False),
                ("derive_market_context", [py, "scripts/derive_market_context.py", "--db", db_path, "--date", selected_date, "--out", report("market_context_latest.json")], False),
                # LHB was orphaned in the phase-mode migration (fetch_all.py
                # imports collect_all_lhb but the --only-market path returns
                # before any call), leaving lhb_* frozen at 2026-07-08.
                ("collect_lhb_daily", [py, "scripts/collect_lhb_daily.py", "--db", db_path, "--date", selected_date, "--out", report("lhb_collection_latest.md")], False),
                # One full-market request replaces the retired per-stock
                # auction/tick and bidding-anomaly fan-out.  It writes both
                # the normalized auction sequence and final matched snapshot.
                ("collect_auction_market_daily", [py, "scripts/collect_auction_market_daily.py", "--db", db_path, "--date", selected_date, "--out", report("auction_market_collection_latest.json")], False),
                # On-the-LHB probability predictions (previously unreachable:
                # only wired behind fetch_all.py's non --only-market path).
                ("collect_advanced_lhb_daily", [py, "scripts/collect_advanced_lhb_daily.py", "--db", db_path, "--date", selected_date, "--out", report("advanced_lhb_collection_latest.md")], False),
                # Northbound capital previously lived only in the staged
                # scheduler (never invoked by the production phases), freezing
                # multi_source_observation at 2026-07-14.
                ("collect_northbound_daily", [py, "scripts/collect_northbound_daily.py", "--db", db_path, "--date", selected_date, "--out", report("northbound_collection_latest.md")], False),
                # Real index klines (full-history endpoint, idempotent) --
                # previously unreachable because the scheduler always runs
                # fetch_all.py with --only-market, which returns early.
                ("collect_index_kline_daily", [py, "scripts/collect_index_kline_daily.py", "--db", db_path, "--date", selected_date, "--out", report("index_kline_collection_latest.md")], False),
                # xiaodefa relay: official chips / margin / unlock calendar /
                # HK-connect holdings. Range kinds (hsgt/ggt/float) tolerate a
                # forward window because future rows simply return empty.
                ("collect_xiaodefa", [py, "scripts/collect_xiaodefa.py", "--db", db_path,
                 "--trade-date", selected_date,
                 "--start-date", selected_date,
                 "--end-date", (date.fromisoformat(selected_date) + timedelta(days=14)).strftime("%Y-%m-%d"),
                 "--kinds", "cyq,margin,float,premarket,kpl,hk_hold"], False),
                # L2 is an intraday-only source.  After the market closes the
                # upstream endpoint normally returns an empty payload, so the
                # close phase reuses the last same-day intraday snapshot.
                ("collect_executable_quotes", [py, "scripts/collect_executable_quotes.py", "--db", db_path, "--date", selected_date,
                 "--limit", str(signal_limit), "--auto-boost-if-kpl-stale"], False),
                # P1 review enhancement is bounded and failure-isolated.  The
                # parent already owns PipelineLock, so the child must not try
                # to acquire a second lock for the same database.
                ("collect_review_supplement", [py, "scripts/collect_review_supplement.py", "--db", db_path,
                 "--date", selected_date, "--max-sectors", "20", "--assume-pipeline-lock",
                 "--out", report("review_supplement_latest.json")], False),
            ]
        elif phase == "supplemental":
            collection_steps = [
                ("collect_lhb_daily", [py, "scripts/collect_lhb_daily.py", "--db", db_path, "--date", selected_date, "--out", report("lhb_collection_latest.md")], False),
                ("collect_auction_market_daily", [py, "scripts/collect_auction_market_daily.py", "--db", db_path, "--date", selected_date, "--out", report("auction_market_collection_latest.json")], False),
                ("collect_index_kline_daily", [py, "scripts/collect_index_kline_daily.py", "--db", db_path, "--date", selected_date, "--out", report("index_kline_collection_latest.md")], False),
                ("collect_xiaodefa_critical", [py, "scripts/collect_xiaodefa.py", "--db", db_path, "--trade-date", selected_date,
                 "--start-date", selected_date, "--end-date", selected_date, "--kinds", "cyq,margin,margin_detail"], False),
            ]
        elif phase == "history":
            start = history_start or "20260101"
            end = history_end or selected_date.replace("-", "")
            collection_steps = [
                ("backfill_2026_tushare", [py, "scripts/backfill_2026_tushare.py", "--db", db_path, "--start-date", start, "--end-date", end, "--max-days", str(max(0, history_max_days)), "--report", report("tushare_2026_backfill_latest.md")], False),
                ("backfill_2026_ths_concepts", [py, "scripts/backfill_2026_ths_concepts.py", "--db", db_path, "--start-date", start, "--end-date", end, "--mode", "full", "--max-member-pages", "0", "--report", report("ths_2026_concepts_latest.md")], False),
                ("run_staged_after_close", [py, "scripts/run_staged_multisource.py", "--db", db_path, "--date", selected_date, "--stage", "after_close", "--start", start, "--end", end, "--budget-seconds", "300", "--report", report("multisource_after_close_latest.md")], False),
            ]
        else:
            raise ValueError(f"Unsupported collection phase: {phase}")
        steps.extend(collection_steps)

    if phase == "history":
        steps.extend([
            ("build_data_catalog", [py, "scripts/build_data_catalog.py", "--db", db_path], False),
            ("audit_data_quality", [py, "scripts/audit_data_quality.py", "--db", db_path, "--schema", "schema.py", "--out", report("data_quality_history_latest.md")], False),
        ])
        return steps
    if phase == "supplemental":
        steps.append(("build_normalized_views", [py, "scripts/build_normalized_views.py", "--db", db_path], False))
        return steps
    selected_phase = phase or "close"
    age = {"auction": 300, "intraday": 600, "close": CLOSE_READINESS_MAX_AGE_SECONDS}[selected_phase]
    steps.append(("build_normalized_views", [py, "scripts/build_normalized_views.py", "--db", db_path], False))
    if selected_phase == "intraday" and include_collection:
        steps.append(("collect_executable_quotes", [py, "scripts/collect_executable_quotes.py", "--db", db_path,
                      "--date", selected_date, "--limit", str(signal_limit), "--auto-boost-if-kpl-stale"], False))
    if selected_phase == "close":
        steps.append(("reconcile_independent_stock_flow", [py, "scripts/reconcile_independent_stock_flow.py",
                      "--db", db_path, "--date", selected_date, "--out", report("independent_stock_flow_reconciliation_latest.md")], False))
    if selected_phase != "auction":
        steps.extend([
            ("audit_multisource_readiness", [py, "scripts/audit_multisource_readiness.py", "--db", db_path,
             "--as-of", selected_date, "--out", report("multisource_readiness_latest.md")], False),
            ("check_capital_flow_health", [py, "scripts/check_capital_flow_health.py", "--db", db_path,
             "--date", selected_date, "--max-age-seconds", str(age), "--min-coverage-pct", "99.5",
             "--out", report("capital_flow_freshness_latest.md"),
             *(["--as-of", as_of_time] if as_of_time else [])], False),
        ])
    steps.append(("check_data_readiness", [py, "scripts/check_data_readiness.py", "--db", db_path,
        "--date", selected_date, "--stage", selected_phase, "--gate", "data", "--max-age-seconds", str(age),
        "--out", report("data_readiness_"+selected_phase+"_latest.md"),
        *(["--as-of", as_of_time] if as_of_time else [])], False))
    # No signal/action writer, report renderer, model refresh or old dashboard
    # remains in this plan. The daily workspace owns all user publications.
    return steps


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
            raise ValueError("collector environment dependency check failed; no collection performed")
    from trade_system.pipeline_runtime import PipelineLock, PipelineAlreadyRunning, RunManifest
    from trade_system.trading_calendar import trading_session_status
    manifest = RunManifest(args.reports_dir, run_id, args.trade_date, selected_phase)
    manifest.data.update(scope="transitional_market_collection_only", user_pages_published=False,
                         collector_contract_sha256=args.collector_contract_sha256, execution_ready=False)
    manifest.write()
    try:
        with PipelineLock(args.db, run_id):
            session = trading_session_status(args.db, args.trade_date)
            if selected_phase != "history" and not args.skip_collect and session.state != "open":
                state = "skipped_market_closed" if session.state == "closed" else "blocked_calendar_unverified"
                manifest.finish(state, session.reason)
                print("MARKET_CLOSED" if session.state == "closed" else "MARKET_CALENDAR_UNVERIFIED")
                return 0 if session.state == "closed" else 2
            # Existing collector code owns schema setup. The new adapter never
            # weakens legacy_connect's primary/V2 write barrier.
            bootstrap = ("import sys; from trade_system.schema import init_schema; "
                         "from base import connect_duckdb; c=connect_duckdb(sys.argv[1]); "
                         "init_schema(c); c.close()")
            initialization = subprocess.run([python, "-X", "utf8", "-c", bootstrap, str(args.db)],
                cwd=backend, capture_output=True, timeout=args.step_timeout, env=_utf8_subprocess_env())
            if initialization.returncode:
                raise ValueError("collector schema initialization failed: "+initialization.stderr.decode("utf-8", "backslashreplace")[-500:])
            failed = []
            for name, command, _ in plan:
                from trade_system.collection_profiles import task_due
                due, reason = task_due(args.db, args.trade_date, name, phase=selected_phase)
                if not due:
                    manifest.add_step(name, "skipped", command, reason=reason)
                    continue
                started = datetime.now()
                log = artifact_dir / (name+".log")
                manifest.upsert_step(name, "running", command, started_at=started.isoformat(), log_path=str(log))
                try:
                    process = subprocess.run(command, cwd=backend, capture_output=True,
                        env=_utf8_subprocess_env(), timeout=args.step_timeout)
                    out, bad_out = _decode_process_bytes(process.stdout, "stdout")
                    err, bad_err = _decode_process_bytes(process.stderr, "stderr")
                    code = process.returncode or (-2 if bad_out or bad_err else 0)
                except subprocess.TimeoutExpired as exc:
                    out, _ = _decode_process_bytes(exc.stdout, "stdout")
                    err, _ = _decode_process_bytes(exc.stderr, "stderr")
                    code = -1
                    err += "\nBounded collector timeout"
                log.write_text("[stdout]\n"+out+"\n[stderr]\n"+err, encoding="utf-8")
                status = "completed" if code == 0 else "degraded"
                manifest.upsert_step(name, status, command, return_code=code, log_path=str(log),
                                     duration_seconds=round((datetime.now()-started).total_seconds(), 3))
                if code:
                    failed.append(name)
                # Independent data sources still run after one provider fails.
                # No failed run is reported as a successful publication.
            manifest.finish("completed_with_degradation" if failed else "completed", ",".join(failed) or None)
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
