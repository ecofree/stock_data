"""Resumable, date-batched 2026 TuShare history backfill."""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.tushare_history import TushareHistoryCollector, render_report  # noqa: E402
from trade_system.pipeline_runtime import PipelineLock  # noqa: E402


def run_local_valuation_session(db, trade_date, *, report, owner, workspace=None,
                                manifest_path=None, manifest_sha256=None, codes=None,
                                deadline_epoch=None):
    """Use the caller's held guard; a boolean/environment flag cannot replace it.

    Standalone CLI calls acquire their own owner. The integrated runner passes
    its actual current owner in-process, so no child attempts to reacquire the
    same permanent guard and no unverified assume-lock path is introduced.
    """
    db = Path(db).resolve()
    expected_lock = db.with_name(db.name+'.pipeline.lock')
    if not isinstance(owner, PipelineLock) or owner.path != expected_lock or owner._guard.fd is None:
        raise ValueError('daily local intake requires the caller-owned database guard')
    metadata = json.loads(expected_lock.read_text(encoding='utf-8'))
    if (metadata.get('lock_protocol') != 'os_handle_v2' or metadata.get('pid') != os.getpid()
            or metadata.get('run_id') != owner.run_id):
        raise ValueError('daily local intake owner metadata mismatch')
    try:
        if deadline_epoch is not None and (not math.isfinite(deadline_epoch) or time.time() >= deadline_epoch):
            raise TimeoutError('daily local intake phase deadline exhausted')
        if workspace and (manifest_path or manifest_sha256):
            raise ValueError('use one explicit daily manifest or workspace intake location')
        if workspace:
            directory = Path(workspace)/'valuation-intake'/trade_date.replace('-', '')
            manifest, fingerprint = directory/'manifest.json', directory/'manifest.sha256'
            if manifest.exists() != fingerprint.exists():
                raise ValueError('daily intake manifest and reviewed SHA must both exist')
            if manifest.exists():
                if fingerprint.stat().st_size > 128:
                    raise ValueError('daily intake SHA file exceeds size bound')
                manifest_path, manifest_sha256 = manifest, fingerprint.read_text(encoding='utf-8').strip()
        with TushareHistoryCollector(db, local_only=True) as collector:
            result = collector.receive_valuation_session(trade_date, codes,
                manifest_path=manifest_path, manifest_sha256=manifest_sha256,
                deadline_epoch=deadline_epoch)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result = dict(schema='valuation_daily_session_v1',trade_date=trade_date,
            status='deferred' if isinstance(exc, TimeoutError) else 'intake_errors',
            workflow_completed=False,valuation_complete=False,
            intake_errors=[{'reason':str(exc)}],market_requests=0,raw_daily_basic_overwritten=False)
    out = Path(report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch TuShare 2026 daily/basic/moneyflow history with checkpoints.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--start-date", default="20260101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--datasets", default="stock_basic,daily,daily_basic,adj_factor,moneyflow,industry_flow",
                        help="Comma-separated: stock_basic,daily,daily_basic,adj_factor,moneyflow,industry_flow,index_daily")
    parser.add_argument("--stock-codes", help="Explicit comma-separated stock scope; only missing instrument/sessions are fetched.")
    parser.add_argument("--index-codes", help="Explicit comma-separated index scope required for index_daily.")
    parser.add_argument("--plan-only", action="store_true", help="Show gaps from the verified local calendar without network requests.")
    parser.add_argument("--max-days", type=int, default=0, help="Limit this invocation; 0 means all open dates.")
    parser.add_argument("--gap-only", action="store_true", help="Only process dates whose requested dataset is not complete.")
    parser.add_argument("--budget-seconds", type=float, default=300.0)
    parser.add_argument("--request-timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--retry-passes",
        type=int,
        default=1,
        help="Retry only failed date/dataset checkpoints after the first pass.",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=15.0,
        help="Backoff before each failed-checkpoint retry pass.",
    )
    parser.add_argument("--batch-limit", type=int, default=5000)
    parser.add_argument("--moneyflow-page-size", type=int, default=1000)
    parser.add_argument("--force", action="store_true", help="Re-fetch successful checkpoints.")
    parser.add_argument("--report", default="reports/tushare_2026_backfill_latest.md")
    valuation = parser.add_mutually_exclusive_group()
    valuation.add_argument('--valuation-prepare', action='store_true',
        help='Offline daily valuation worklist; original finance is reusable but new session evidence and review are required.')
    valuation.add_argument('--valuation-diagnostic', action='store_true',
        help='Explicit newly authorized diagnostic only: max two requests per endpoint/six total; JSON report.')
    valuation.add_argument('--valuation-evidence', help='Explicitly reviewed valuation bundle to import locally; no network.')
    valuation.add_argument('--valuation-session', action='store_true',
        help='Daily local reviewed intake and pending worklist; never acquires finance or fabricates a review.')
    parser.add_argument('--valuation-evidence-sha256', help='Approved SHA256 of the exact reviewed bundle.')
    parser.add_argument('--valuation-workspace', help='Workspace with valuation-intake/YYYYMMDD/manifest.json and manifest.sha256.')
    parser.add_argument('--valuation-intake-manifest', help='Exact local daily intake manifest, already reviewed.')
    parser.add_argument('--valuation-intake-sha256', help='Approved SHA256 of that exact daily manifest.')
    args = parser.parse_args()
    if args.valuation_prepare or args.valuation_diagnostic or args.valuation_evidence or args.valuation_session:
        if args.plan_only or args.start_date != args.end_date:
            parser.error('valuation mode requires one explicit session and cannot combine with plan-only')
        if (args.valuation_prepare or args.valuation_diagnostic) and not args.stock_codes:
            parser.error('valuation preparation/diagnostic requires explicit stock codes')
        if args.valuation_evidence and not args.valuation_evidence_sha256:
            parser.error('valuation evidence requires its exact reviewed SHA256')
        if (args.valuation_intake_manifest or args.valuation_intake_sha256 or args.valuation_workspace) and not args.valuation_session:
            parser.error('daily intake options require valuation-session')
        if args.valuation_workspace and (args.valuation_intake_manifest or args.valuation_intake_sha256):
            parser.error('use one explicit daily manifest or workspace intake location')
        if bool(args.valuation_intake_manifest) != bool(args.valuation_intake_sha256):
            parser.error('daily intake manifest requires its exact reviewed SHA256')
        if args.valuation_session:
            with PipelineLock(args.db, 'valuation-session-'+uuid4().hex) as owner:
                result = run_local_valuation_session(args.db, args.start_date, report=args.report, owner=owner,
                    workspace=args.valuation_workspace, manifest_path=args.valuation_intake_manifest,
                    manifest_sha256=args.valuation_intake_sha256,
                    codes=[c.strip() for c in args.stock_codes.split(',') if c.strip()] if args.stock_codes else None)
            print(f"valuation_session workflow_completed={result['workflow_completed']} "
                  f"valuation_complete={result['valuation_complete']} report={args.report}")
            return 0 if result['workflow_completed'] else 2
        if args.valuation_prepare:
            with TushareHistoryCollector(args.db, offline=True) as collector:
                result = collector.prepare_valuation_reviews(args.start_date,
                    [c.strip() for c in args.stock_codes.split(',') if c.strip()])
        else:
            with PipelineLock(args.db, 'valuation-'+uuid4().hex), TushareHistoryCollector(
                    args.db, request_timeout=args.request_timeout, retries=1,
                    budget_seconds=args.budget_seconds, **({'local_only': True} if args.valuation_evidence else {})) as collector:
                if args.valuation_evidence:
                    result = collector.import_valuation_reviews(args.valuation_evidence,
                        args.valuation_evidence_sha256, args.start_date)
                else:
                    result = collector.collect_valuation_inputs(args.start_date,
                        [c.strip() for c in args.stock_codes.split(',') if c.strip()])
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')
        rows = result.get('completion', {}).get('rows', {})
        complete = bool(args.valuation_evidence) or (bool(rows) and all(r.get('valuation_eligible') for r in rows.values()))
        if args.valuation_prepare:
            complete = all(r.get('qualified_core') for r in result['rows'].values())
        print(f'valuation_inputs complete={complete} report={out}')
        return 0 if complete else 2
    datasets = [item.strip() for item in args.datasets.split(",") if item.strip()]
    with TushareHistoryCollector(
        args.db,
        request_timeout=args.request_timeout,
        retries=args.retries,
        batch_limit=args.batch_limit,
        moneyflow_page_size=args.moneyflow_page_size,
        budget_seconds=args.budget_seconds,
        offline=args.plan_only,
    ) as collector:
        result = collector.run(
            args.start_date,
            args.end_date,
            datasets=datasets,
            max_days=args.max_days or None,
            force=args.force,
            gap_only=args.gap_only,
            retry_passes=args.retry_passes,
            retry_delay_seconds=args.retry_delay_seconds,
            stock_codes=[c.strip() for c in args.stock_codes.split(",") if c.strip()] if args.stock_codes is not None else None,
            index_codes=[c.strip() for c in args.index_codes.split(",") if c.strip()] if args.index_codes is not None else None,
            plan_only=args.plan_only,
        )
    report = render_report(args.db, result, args.report)
    summary = {}
    for item in result["results"]:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    print(
        f"tushare_history start={result['start_date']} end={result['end_date']} "
        f"dates={len(result['dates'])} results={len(result['results'])} summary={summary} report={report}"
    )
    incomplete = summary.get("error", 0) + summary.get("budget_exhausted", 0)
    return 0 if not incomplete else 2


if __name__ == "__main__":
    raise SystemExit(main())
