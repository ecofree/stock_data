"""Collection profiles for the operator-facing market-data scheduler.

The project has many providers, but a trading-day run should only touch the
sources needed by the current decision window.  This module is deliberately
small and dependency-light: it describes the phase/source matrix and provides
the freshness gate used by ``run_integrated_daily.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import sys
from trade_system.config import default_trade_date
from pathlib import Path
from typing import Any

import duckdb

from trade_system.ths_quality import canonical_ths_snapshot
from trade_system.time_utils import as_local_naive


PHASES = ("auction", "intraday", "close", "supplemental", "history")


@dataclass(frozen=True)
class ProfileTask:
    name: str
    source: str
    cadence_seconds: int | None
    purpose: str
    network: bool = True


HISTORY_SUPPLEMENT_TYPES = ("financials", "statements", "margin_trading", "dragon_tiger_daily", "northbound_hist")


CommandStep = tuple[str, list[str], bool]
TUSHARE_GAPFILL_LOOKBACK_DAYS = 10
CLOSE_READINESS_MAX_AGE_SECONDS = 7200

_TASKS = {
    "build_auction_evidence": ProfileTask("build_auction_evidence", "local auction snapshots", None, "normalize retained auction observations", network=False),
    'collect_market_context': ProfileTask('collect_market_context', 'KPL market/rise-fall', 300, 'market regime and auction context', network=True),
    'collect_realtime_limit_pool': ProfileTask('collect_realtime_limit_pool', 'KPL L2 realtime/ladder', 180, 'same-day executable limit-up pool', network=True),
    'collect_auction_evidence': ProfileTask('collect_auction_evidence', 'KPL /auction/market + Tencent fallback', 180, 'full-market auction sequence and final match evidence', network=True),
    'collect_intraday_stock_flow_market': ProfileTask('collect_intraday_stock_flow_market', 'Eastmoney push2 clist', 300, 'full-market stock capital flow', network=True),
    'collect_executable_quotes': ProfileTask('collect_executable_quotes', 'Tencent qt.gtimg.cn spot', 180, 'candidate-only live prices for entry-executable signals when clist is delayed-only', network=True),
    'collect_l2_focus': ProfileTask('collect_l2_focus', 'KPL /l2/stock-intraday (candidates)', 300, 'bounded L2 price curves; phase mode never runs full L2', network=True),
    'collect_intraday_sector_flow_full': ProfileTask('collect_intraday_sector_flow_full', 'Eastmoney sector pages + TuShare/THS aggregate', 300, 'full-sector capital flow refreshed after L2', network=True),
    'sync_tushare_close': ProfileTask('sync_tushare_close', 'TuShare relay date batches', 3600, 'same-day daily/basic/adjustment/money-flow facts', network=True),
    'collect_hithink_limit_pool_daily': ProfileTask('collect_hithink_limit_pool_daily', 'HiThink official limit-up pool', 3600, 'same-day close limit-up facts and reasons', network=True),
    'collect_kpl_stock_flow_focus': ProfileTask('collect_kpl_stock_flow_focus', 'KPL advanced/zjmm-min', 3600, 'bounded independent money-flow confirmation for candidate stocks', network=True),
    'collect_review_supplement': ProfileTask('collect_review_supplement', 'KPL bounded P1 review supplement', 86400, 'daily review enhancement; never a close gate', network=True),
    'collect_auction_market_daily': ProfileTask('collect_auction_market_daily', 'KPL /auction/market', 3600, 'full-market after-close auction evidence', network=True),
    'collect_lhb_daily': ProfileTask('collect_lhb_daily', 'KPL LHB', None, 'explicit late disclosure; collector coverage cache', network=True),
    'collect_index_kline_daily': ProfileTask('collect_index_kline_daily', 'KPL index', None, 'bounded index history; collector coverage cache', network=True),
    'collect_xiaodefa_critical': ProfileTask('collect_xiaodefa_critical', 'TuShare relay', None, 'late chips and margin evidence', network=True),
    'backfill_2026_tushare': ProfileTask('backfill_2026_tushare', 'TuShare relay', None, 'resumable daily/basic/moneyflow history', network=True),
    'backfill_2026_ths_concepts': ProfileTask('backfill_2026_ths_concepts', 'THS web pages', 604800, 'weekly concept catalogue and constituents snapshot', network=True),
    'collect_history_supplement': ProfileTask('collect_history_supplement', 'provider fallback graph', None, 'financials, statements, margin and historical northbound', network=True),
    'collect_ths_concepts_api': ProfileTask('collect_ths_concepts_api', 'HiThink official concept catalogue', 86400, 'qualified same-date catalogue and members', network=True),
    'collect_advanced_lhb_daily': ProfileTask('collect_advanced_lhb_daily', 'KPL advanced LHB', None, 'bounded disclosed LHB supplement', network=True),
    'collect_northbound_daily': ProfileTask('collect_northbound_daily', 'provider-defined northbound', None, 'unverified product; explicit bounded collection only', network=True),
    'collect_xiaodefa': ProfileTask('collect_xiaodefa', 'TuShare via xiaodefa', None, 'declared date/range kinds; bounded collector budget', network=True),
    'derive_market_context': ProfileTask('derive_market_context', 'local stored facts', None, 'local projection or quality check', network=False),
    'build_normalized_views': ProfileTask('build_normalized_views', 'local stored facts', None, 'local projection or quality check', network=False),
    'reconcile_independent_stock_flow': ProfileTask('reconcile_independent_stock_flow', 'local stored facts', None, 'local projection or quality check', network=False),
    'audit_multisource_readiness': ProfileTask('audit_multisource_readiness', 'local stored facts', None, 'local projection or quality check', network=False),
    'check_capital_flow_health': ProfileTask('check_capital_flow_health', 'local stored facts', None, 'local projection or quality check', network=False),
    'check_data_readiness': ProfileTask('check_data_readiness', 'local stored facts', None, 'local projection or quality check', network=False),
    'build_data_catalog': ProfileTask('build_data_catalog', 'local stored facts', None, 'local projection or quality check', network=False),
    'audit_data_quality': ProfileTask('audit_data_quality', 'local stored facts', None, 'local projection or quality check', network=False),
}

# Cadence differences only; phase membership comes from the command plan.
_PHASE_CADENCE = {'auction': {}, 'intraday': {}, 'close': {'collect_market_context': 3600, 'collect_realtime_limit_pool': 3600, 'collect_intraday_stock_flow_market': 3600, 'collect_intraday_sector_flow_full': 3600, 'collect_executable_quotes': 3600}, 'supplemental': {'collect_auction_market_daily': None}, 'history': {}}

def validate_plan(steps):
    names = [name for name, _, _ in steps]
    if len(names) != len(set(names)) or any(name not in _TASKS for name in names):
        raise ValueError("duplicate or unregistered collection task")
    return steps

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
    close_facts = ("sync_tushare_close", [py, "scripts/backfill_2026_tushare.py", "--db", db_path,
        "--start-date", (date.fromisoformat(selected_date) - timedelta(days=TUSHARE_GAPFILL_LOOKBACK_DAYS)
                         if phase == 'close' else date.fromisoformat(selected_date)).strftime('%Y%m%d'),
        "--end-date", selected_date.replace('-', ''),
        "--datasets", "daily,daily_basic,adj_factor,moneyflow,industry_flow", "--gap-only", "--max-days", "1",
        "--retry-passes", "0" if phase == 'supplemental' else "1", "--retry-delay-seconds", "2.0",
        "--report", report("tushare_close_latest.md")], False)
    steps: list[CommandStep] = []
    if include_collection and phase is not None:
        # Phase mode is intentionally narrow and is the only collection path.
        if phase == "auction":
            collection_steps = [
                ("collect_market_context", [py, "collectors/collect_market.py", "--db", db_path, "--date", selected_date], False),
                ("collect_realtime_limit_pool", [py, "scripts/collect_realtime_limit_pool.py", "--db", db_path, "--date", selected_date, "--out", report("realtime_candidate_pool_latest.md")], False),
                ("collect_auction_evidence", [py, "scripts/collect_auction_evidence.py", "--db", db_path, "--date", selected_date, "--max-stocks", str(signal_limit), "--out", report("auction_collection_latest.json")], False),
                ("build_auction_evidence", [py, "scripts/build_auction_evidence.py", "--db", db_path, "--trade-date", selected_date, "--out", report("auction_evidence_latest.md")], False),
            ]
        elif phase == "intraday":
            collection_steps = [
                ("collect_market_context", [py, "collectors/collect_market.py", "--db", db_path, "--date", selected_date], False),
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
                ("collect_market_context", [py, "collectors/collect_market.py", "--db", db_path, "--date", selected_date], False),
                # Daily ingestion writes raw facts; normalization projects them without copies.
                close_facts,
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
                close_facts,
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
                ("collect_history_supplement", [py, "scripts/collect_multisource.py", "--db", db_path,
                 "--date", selected_date, "--start", start, "--end", end,
                 "--types", ",".join(HISTORY_SUPPLEMENT_TYPES), "--universe-table", "stock_basic",
                 "--max-stocks", "20", "--periods", "8", "--resume", "--budget-seconds", "300",
                 "--report", report("multisource_after_close_latest.md")], False),
            ]
        else:
            raise ValueError(f"Unsupported collection phase: {phase}")
        steps.extend(collection_steps)

    if phase == "history":
        steps.extend([
            ("build_data_catalog", [py, "scripts/build_data_catalog.py", "--db", db_path], False),
            ("audit_data_quality", [py, "scripts/audit_data_quality.py", "--db", db_path, "--schema", "trade_system/schema.py", "--out", report("data_quality_history_latest.md")], False),
        ])
        return validate_plan(steps)
    if phase == "supplemental":
        steps.append(("build_normalized_views", [py, "scripts/build_normalized_views.py", "--db", db_path], False))
        return validate_plan(steps)
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
    return validate_plan(steps)


PROFILE_TASKS = {phase: tuple(replace(_TASKS[name],
    cadence_seconds=_PHASE_CADENCE.get(phase, {}).get(name, _TASKS[name].cadence_seconds))
    for name, _, _ in command_plan("unused", "2000-01-01", include_collection=True, phase=phase))
    for phase in PHASES}


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
    if value != "auto":
        raise ValueError(f"unsupported collection phase: {phase}")
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


def official_pool_checkpoint(con, trade_date):
    """Only complete native publication with matching stored rows is reusable."""
    if not all(_table_exists(con, name) for name in ('history_fetch_checkpoint','official_limit_pool')):
        return None
    return _latest(con, """SELECT updated_at,status FROM history_fetch_checkpoint c
        WHERE dataset='hithink_limit_pool' AND c.trade_date=? AND page_no=0
        AND status='success' AND last_error IS NOT NULL
        AND rows_written=(SELECT count(*) FROM official_limit_pool p
            WHERE p.trade_date=c.trade_date AND p.source='hithink')""", [trade_date])


def publication_readiness(db_path: str | Path, trade_date: str) -> dict:
    """Read-only publication prerequisites, independent of optional supplements."""
    from trade_system.tushare_history import TushareHistoryCollector
    result = {'passed': False, 'trade_date': trade_date, 'price_rows': 0,
              'scope': 'market_page_prerequisites_not_full_data_or_account_acceptance'}
    try:
        with TushareHistoryCollector(db_path, offline=True) as collector:
            if not collector._reference_version():
                return dict(result, reason='stock_reference_unqualified')
            con = collector.store.conn
            checkpoint = con.execute("SELECT status,rows_written FROM history_fetch_checkpoint "
                "WHERE dataset='daily' AND trade_date=? AND page_no=0", [trade_date]).fetchone()
            if not checkpoint or checkpoint[0] != 'success' or checkpoint[1] <= 0:
                return dict(result, reason='daily_snapshot_unqualified')
            rows, identities = con.execute("SELECT count(*),count(DISTINCT stock_code) FROM v_kline_daily WHERE trade_date=? AND close>0 "
                "AND isfinite(close) AND isfinite(change_pct) AND provider IS NOT NULL "
                "AND adjustment IS NOT NULL AND volume_unit IS NOT NULL AND amount_unit IS NOT NULL",
                [trade_date]).fetchone()
            result['price_rows'] = rows
            if rows != checkpoint[1] or identities != rows:
                return dict(result, reason='canonical_prices_incomplete')
            return dict(result, passed=True, reason='current_qualified_prices_available')
    except (ValueError, duckdb.Error) as exc:
        return dict(result, reason='publication_inputs_unavailable', error_type=type(exc).__name__)


def task_due(db_path: str | Path, trade_date: str, task_name: str,
             *, phase: str | None = None, now: datetime | None = None, force: bool = False) -> tuple[bool, str]:
    """Return ``(due, reason)`` without making any network request.

    A retry cooldown never certifies freshness or grants an unknown task.
    """
    tasks = phase_tasks(phase) if phase is not None else tuple(_TASKS.values())
    task = next((item for item in tasks if item.name == task_name), None)
    if task is None:
        raise ValueError(f"unregistered task in phase {phase}: {task_name}")
    if not task.network:
        return True, "declared local computation"
    if force:
        return True, "explicit forced request"
    if task.cadence_seconds is None:
        return True, "declared bounded collector/checkpoint policy"
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
            if len(sources) != 2:
                return True, "market context tables missing"
            union = " UNION ALL ".join(sources)
            row = _latest(con, f"SELECT CASE WHEN count(fetched_at)=2 THEN min(fetched_at) END, 'ok' FROM ({union})", [trade_date] * len(sources))
        elif task_name == "collect_realtime_limit_pool":
            if not _table_exists(con, "realtime_candidate_pool_snapshot"):
                return True, "candidate snapshot missing"
            row = _latest(con, "SELECT fetched_at, status FROM realtime_candidate_pool_snapshot WHERE CAST(trade_date AS VARCHAR)=? ORDER BY fetched_at DESC LIMIT 1", [trade_date])
        elif task_name == "collect_auction_evidence":
            if not _table_exists(con, "auction_collection_batch"):
                return True, "auction batch checkpoint missing"
            row = _latest(con, "SELECT attempted_at, status FROM auction_collection_batch WHERE CAST(trade_date AS VARCHAR)=?", [trade_date])
        elif task_name == "collect_hithink_limit_pool_daily":
            row = official_pool_checkpoint(con, trade_date)
        elif task_name == "collect_ths_concepts_api":
            snapshot = canonical_ths_snapshot(con, trade_date)
            if snapshot and str(snapshot['snapshot_date'])[:10] == trade_date:
                row = (datetime.fromisoformat(trade_date+'T15:00:00'), 'success')
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
        elif task_name == "collect_auction_market_daily":
            if not _table_exists(con, "auction_collection_batch"):
                return True, "auction batch checkpoint missing"
            row = _latest(
                con,
                "SELECT attempted_at, status FROM auction_collection_batch "
                "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY attempted_at DESC LIMIT 1",
                [trade_date],
            )
        elif task_name == "collect_review_supplement":
            if not _table_exists(con, "review_supplement_batch"):
                return True, "review supplement checkpoint missing"
            row = _latest(
                con,
                "SELECT attempted_at, status FROM review_supplement_batch "
                "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY attempted_at DESC LIMIT 1",
                [trade_date],
            )
        if not row or row[0] is None:
            return True, "no same-date snapshot"
        fetched = row[0]
        if not isinstance(fetched, datetime):
            fetched = as_local_naive(str(fetched).replace("Z", "+00:00"))
        status = str(row[1]).lower() if len(row) > 1 and row[1] is not None else ""
        ttl = task.cadence_seconds
        retrying = status in {"partial", "failed", "empty", "stale", "error", "running"}
        if retrying:
            ttl = max(60, min(ttl // 2, 86400))
        age = max(0.0, (as_local_naive(current) - as_local_naive(fetched)).total_seconds())
        if age < ttl:
            return False, f"{'retry cooldown' if retrying else 'fresh'} age={int(age)}s ttl={ttl}s status={status or 'ok'}"
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
