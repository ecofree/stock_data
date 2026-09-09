"""Ordered, resumable collection of the migrated market-data sources.

The source layer still owns provider fallback.  This module owns *when* a
data type is requested: one task at a time, in a fixed intraday stage, with a
wall-clock budget and a durable checkpoint written before and after every
request.  That keeps a slow or broken endpoint from fanning out into dozens of
simultaneous requests and makes an interrupted run safe to resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from trade_system.multi_source_store import MultiSourceStore, infer_codes, new_run_id
from trade_system.trading_calendar import previous_open_session


STAGE_ORDER = ("premarket", "open", "midday", "close", "after_close")


@dataclass(frozen=True)
class TaskDefinition:
    name: str
    data_type: str
    scope: str = "global"  # global, stock, index
    current_only: bool = False
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageTask:
    stage: str
    name: str
    data_type: str
    scope: str
    asset_code: str
    current_only: bool
    kwargs: dict[str, Any] = field(default_factory=dict)


STAGE_DEFINITIONS: dict[str, tuple[TaskDefinition, ...]] = {
    # Universe and pre-open snapshots are intentionally small and global.
    "premarket": (
        TaskDefinition("stock_basic", "stock_basic", current_only=True),
        TaskDefinition("index_spot", "index_spot", "index", current_only=True),
        TaskDefinition("valuation", "valuation", "stock", current_only=True),
    ),
    "open": (
        TaskDefinition("sector_flow", "sector_flow", current_only=True),
        TaskDefinition("bid_ask", "bid_ask", "stock", current_only=True),
        TaskDefinition("intraday", "intraday", "stock", current_only=True),
        TaskDefinition("northbound", "northbound", current_only=True),
    ),
    "midday": (
        TaskDefinition("stock_flow", "stock_flow", "stock", current_only=True),
        TaskDefinition("sector_flow", "sector_flow", current_only=True),
        TaskDefinition("intraday", "intraday", "stock", current_only=True),
        TaskDefinition("northbound", "northbound", current_only=True),
    ),
    "close": (
        TaskDefinition("kline", "kline", "stock"),
        TaskDefinition("index_kline", "index_kline", "index"),
        TaskDefinition("stock_flow", "stock_flow", "stock", current_only=True),
        TaskDefinition("sector_flow", "sector_flow", current_only=True),
    ),
    "after_close": (
        TaskDefinition("financials", "financials", "stock", kwargs={"periods": 8}),
        TaskDefinition("statements", "statements", "stock", kwargs={"report_type": "lrb", "periods": 8}),
        TaskDefinition("margin_trading", "margin_trading", "stock"),
        TaskDefinition("dragon_tiger_daily", "dragon_tiger_daily", current_only=True),
        TaskDefinition("northbound_hist", "northbound_hist"),
    ),
}


def _iso(value: str | date | None) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return str(value)


def _ymd(value: str) -> str:
    return _iso(value).replace("-", "")


def _recent_session_dates(con) -> set[str]:
    """Dates that may still be legitimately refreshed as current snapshots.

    A close-stage run on the following morning commonly refreshes the prior
    trading session.  Allowing one prior weekday preserves that workflow while
    still rejecting older historical dates (which must use dated collectors).
    """
    today = date.today().isoformat()
    out = {today}
    previous = previous_open_session(con, today)
    if previous:
        out.add(previous)
    return out


def _count_rows(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return 1 if data else 0
    return 0


class StageScheduler:
    """Run source tasks sequentially and persist a checkpoint per task."""

    def __init__(
        self,
        db_path: str | Path,
        trade_date: str | date | None = None,
        stock_codes: Iterable[str] | None = None,
        index_codes: Iterable[str] | None = None,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        max_stocks: int = 20,
        max_sectors: int = 200,
        budget_seconds: float = 120.0,
        fetcher: Callable[..., tuple[Any, dict]] | None = None,
    ):
        self.db_path = str(db_path)
        self.trade_date = _iso(trade_date)
        self.start = _iso(start or self.trade_date)
        self.end = _iso(end or self.trade_date)
        self.max_stocks = max(0, int(max_stocks))
        self.max_sectors = max(1, int(max_sectors))
        self.budget_seconds = max(1.0, float(budget_seconds))
        self.stock_codes = [str(c).strip() for c in (stock_codes or []) if str(c).strip()][: self.max_stocks]
        self.index_codes = [str(c).strip() for c in (index_codes or []) if str(c).strip()]
        self.store = MultiSourceStore(self.db_path, fetcher=fetcher)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "StageScheduler":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _codes(self, scope: str) -> list[str]:
        if scope == "stock":
            if not self.stock_codes:
                self.stock_codes = infer_codes(self.db_path, "stock_basic", "stock_code", self.max_stocks)
            return self.stock_codes[: self.max_stocks]
        if scope == "index":
            return self.index_codes or ["SH000001", "SZ399001", "SZ399006"]
        return [""]

    def _kwargs(self, definition: TaskDefinition) -> dict[str, Any]:
        kwargs = dict(definition.kwargs)
        if definition.data_type in {"kline", "index_kline"}:
            kwargs.update(start=_ymd(self.start), end=_ymd(self.end), fq="qfq", full_history=False)
        elif definition.data_type in {"sector_flow", "stock_flow"}:
            kwargs.update(top_n=self.max_sectors)
        elif definition.data_type in {"intraday", "dragon_tiger_daily"}:
            kwargs["date"] = _ymd(self.trade_date) if definition.data_type == "intraday" else self.trade_date
        elif definition.data_type in {"northbound_hist"}:
            kwargs.update(start=_ymd(self.start), end=_ymd(self.end))
        return kwargs

    def plan(self, stage: str) -> list[StageTask]:
        stage = stage.strip().lower()
        if stage not in STAGE_DEFINITIONS:
            raise ValueError(f"unknown stage: {stage}; choose from {', '.join(STAGE_ORDER)}")
        out: list[StageTask] = []
        for definition in STAGE_DEFINITIONS[stage]:
            for code in self._codes(definition.scope):
                out.append(StageTask(
                    stage=stage,
                    name=definition.name,
                    data_type=definition.data_type,
                    scope=definition.scope,
                    asset_code=code,
                    current_only=definition.current_only,
                    kwargs=self._kwargs(definition),
                ))
        return out

    def _checkpoint(self, task: StageTask) -> tuple[Any, ...] | None:
        return self.store.con.execute(
            "SELECT status, attempts, rows_written, provider, last_error FROM multi_source_task_checkpoint "
            "WHERE trade_date=? AND stage=? AND task_name=? AND data_type=? AND asset_code=?",
            [self.trade_date, task.stage, task.name, task.data_type, task.asset_code],
        ).fetchone()

    def _write_checkpoint(self, *, run_id: str, task: StageTask, status: str, attempts: int = 0,
                          rows_written: int = 0, provider: str | None = None,
                          started_at: datetime | None = None, finished_at: datetime | None = None,
                          last_error: str | None = None) -> None:
        self.store.con.execute(
            "INSERT INTO multi_source_task_checkpoint "
            "(run_id,trade_date,stage,task_name,data_type,asset_code,status,attempts,rows_written,provider,started_at,finished_at,last_error,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (trade_date,stage,task_name,data_type,asset_code) DO UPDATE SET "
            "run_id=excluded.run_id,status=excluded.status,attempts=excluded.attempts,rows_written=excluded.rows_written,"
            "provider=excluded.provider,started_at=excluded.started_at,finished_at=excluded.finished_at,"
            "last_error=excluded.last_error,updated_at=excluded.updated_at",
            [run_id, self.trade_date, task.stage, task.name, task.data_type, task.asset_code,
             status, attempts, rows_written, provider, started_at, finished_at, last_error, datetime.now()],
        )
        self.store.con.commit()

    def _skip(self, run_id: str, task: StageTask, reason: str, *, attempts: int = 0) -> dict[str, Any]:
        now = datetime.now()
        self._write_checkpoint(run_id=run_id, task=task, status="skipped", attempts=attempts,
                               started_at=now, finished_at=now, last_error=reason)
        return {"task": task, "status": "skipped", "rows_written": 0, "provider": None, "error": reason}

    def run(self, stages: Iterable[str] | str, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        selected = [stages] if isinstance(stages, str) else list(stages)
        if not selected:
            selected = list(STAGE_ORDER)
        requested = [str(s).strip().lower() for s in selected]
        if any(stage not in STAGE_DEFINITIONS for stage in requested):
            raise ValueError(f"unknown stage in {requested}; choose from {', '.join(STAGE_ORDER)}")
        # The caller may request a subset in any order, but collection itself
        # always follows the market-day order so a later stage never races a
        # prerequisite stage.
        selected = [stage for stage in STAGE_ORDER if stage in requested]
        tasks: list[StageTask] = []
        for stage in selected:
            tasks.extend(self.plan(stage))
        run_id = new_run_id()
        started_all = datetime.now()
        started_mono = time.monotonic()
        results: list[dict[str, Any]] = []
        budget_exhausted = False
        current_snapshot_dates = _recent_session_dates(self.store.con)

        if dry_run:
            return {"run_id": run_id, "stages": selected, "planned": len(tasks),
                    "tasks": [{"stage": t.stage, "task": t.name, "data_type": t.data_type,
                               "scope": t.scope, "asset_code": t.asset_code, "kwargs": t.kwargs,
                               "status": "planned", "provider": None, "rows_written": 0} for t in tasks],
                    "dry_run": True, "elapsed_seconds": 0.0}

        for index, task in enumerate(tasks):
            checkpoint = None if force else self._checkpoint(task)
            # Only completed data is resumable.  A budget-exhausted or
            # historical-current snapshot marked ``skipped`` must not poison
            # a later run that has a larger budget or a different date.
            if checkpoint and checkpoint[0] in {"success", "refreshed", "fresh"}:
                results.append({"task": task, "status": "skipped", "rows_written": checkpoint[2] or 0,
                                "provider": checkpoint[3], "error": "checkpoint_complete"})
                continue
            if task.current_only and self.trade_date not in current_snapshot_dates:
                results.append(self._skip(run_id, task, "current_snapshot_not_relabelled_for_historical_date",
                                         attempts=(checkpoint[1] if checkpoint else 0) or 0))
                continue
            if time.monotonic() - started_mono >= self.budget_seconds:
                budget_exhausted = True
                for pending in tasks[index:]:
                    results.append(self._skip(run_id, pending, "budget_exhausted"))
                break

            attempts = ((checkpoint[1] if checkpoint else 0) or 0) + 1
            started = datetime.now()
            self._write_checkpoint(run_id=run_id, task=task, status="running", attempts=attempts,
                                   started_at=started)
            try:
                fetch_kwargs = dict(task.kwargs)
                # ``--force`` means a real source refresh, not merely a new
                # scheduler run.  ttl=0 bypasses the resilient cache while
                # preserving provider fallback and rate limiting.
                if force:
                    fetch_kwargs["ttl"] = 0
                data, meta = self.store.fetch(task.data_type, task.asset_code or None, **fetch_kwargs)
                meta = dict(meta or {})
                stored = self.store.store(
                    task.data_type, task.asset_code or None, data, meta,
                    asset_type=("index" if task.scope == "index" else "stock" if task.scope == "stock" else task.data_type),
                    trade_date=self.trade_date,
                )
                status = str(meta.get("status") or stored.get("status") or "failed")
                if data is None or status == "failed":
                    final_status = "failed"
                elif status == "stale":
                    final_status = "stale"
                else:
                    final_status = "success"
                error = str(meta.get("error") or "")[:500] or None
                rows_written = int(stored.get("rows_written") or 0)
                # Generic payloads are still durably stored in
                # multi_source_observation.  Count those rows for visibility,
                # but never count stale fallback rows as newly written data.
                if not stored.get("stale") and rows_written == 0:
                    rows_written = _count_rows(data)
                self._write_checkpoint(run_id=run_id, task=task, status=final_status, attempts=attempts,
                                       rows_written=rows_written,
                                       provider=stored.get("provider") or meta.get("source"),
                                       started_at=started, finished_at=datetime.now(), last_error=error)
                results.append({"task": task, "status": final_status,
                                "rows_written": rows_written,
                                "provider": stored.get("provider") or meta.get("source"), "error": error})
            except Exception as exc:  # one bad endpoint must not abort later stages
                error = str(exc)[:500]
                self._write_checkpoint(run_id=run_id, task=task, status="failed", attempts=attempts,
                                       started_at=started, finished_at=datetime.now(), last_error=error)
                results.append({"task": task, "status": "failed", "rows_written": 0,
                                "provider": None, "error": error})

        # This is a compatibility/recovery path. Keep provider evidence in
        # multi_source_*; the integrated phase owner is the only normal
        # writer allowed to promote rows into canonical core tables.

        finished_all = datetime.now()
        counts = {key: sum(1 for item in results if item["status"] == key)
                  for key in ("success", "stale", "failed", "skipped")}
        providers = sorted({str(item["provider"]) for item in results if item.get("provider")})
        for data_type in sorted({item["task"].data_type for item in results}):
            subset = [item for item in results if item["task"].data_type == data_type]
            self.store.record_run(
                run_id=run_id, started=started_all, finished=finished_all, trade_date=self.trade_date,
                data_type=data_type, asset_scope="staged", requested=len(subset),
                success=sum(item["status"] == "success" for item in subset),
                stale=sum(item["status"] == "stale" for item in subset),
                failed=sum(item["status"] == "failed" for item in subset),
                providers=sorted({str(item["provider"]) for item in subset if item.get("provider")}),
                errors=[item["error"] for item in subset if item.get("error")],
            )
        return {"run_id": run_id, "stages": selected, "planned": len(tasks),
                "completed": counts["success"], "stale": counts["stale"],
                "failed": counts["failed"], "skipped": counts["skipped"],
                "budget_exhausted": budget_exhausted, "providers": providers,
                "tasks": results, "elapsed_seconds": round(time.monotonic() - started_mono, 3),
                "dry_run": False}
