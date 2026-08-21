"""Historical KPL concept/constituent snapshots.

The dated ``/sector/ranking`` endpoint is the project default: it returns the
daily leading concepts and, when available, their constituent stocks in one
request.  Full all-plate membership is intentionally a separate snapshot
mode because ``/sector/all-stocks`` is a current-state endpoint without a
reliable historical date parameter.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any

import duckdb

from base import DuckDBStore, KPLClient
from trade_system.schema import init_schema


def _iso(value: str | date) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    raise ValueError(f"invalid date: {value}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _payload_date(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("trade_date", "date", "as_of", "as_of_date"):
        value = payload.get(key)
        if value:
            try:
                return _iso(value)
            except ValueError:
                continue
    return None


def _weekday_dates(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(_iso(start_date), "%Y-%m-%d").date()
    end = datetime.strptime(_iso(end_date), "%Y-%m-%d").date()
    out = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


class KPLHistoryCollector:
    def __init__(self, db_path: str | Path, *, request_timeout: float = 10,
                 max_attempts: int = 1, budget_seconds: float = 300):
        self.db_path = str(db_path)
        self.store = DuckDBStore(self.db_path)
        init_schema(self.store.conn)
        self.client = KPLClient(request_timeout=request_timeout, max_attempts=max_attempts,
                                total_budget_seconds=budget_seconds)
        self.started = time.monotonic()

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "KPLHistoryCollector":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _checkpoint(self, dataset: str, trade_date: str, status: str, *, rows: int = 0,
                    attempts: int = 0, error: str = "") -> None:
        self.store.conn.execute(
            "INSERT INTO history_fetch_checkpoint(dataset,trade_date,page_no,status,rows_written,attempts,last_error,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(dataset,trade_date,page_no) DO UPDATE SET status=excluded.status,"
            "rows_written=excluded.rows_written,attempts=excluded.attempts,last_error=excluded.last_error,updated_at=excluded.updated_at",
            [dataset, _iso(trade_date), 0, status, rows, attempts, error[:500], datetime.now()],
        )
        self.store.conn.commit()

    def _done(self, dataset: str, trade_date: str, force: bool) -> bool:
        if force:
            return False
        row = self.store.conn.execute(
            "SELECT status FROM history_fetch_checkpoint WHERE dataset=? AND trade_date=? AND page_no=0",
            [dataset, _iso(trade_date)],
        ).fetchone()
        return bool(row and row[0] == "success")

    def collect_ranking(self, trade_date: str) -> int:
        payload = self.client.get("/sector/ranking", {"date": trade_date}, critical=True)
        sectors = payload.get("sectors", []) if isinstance(payload, dict) else []
        if not sectors:
            return 0
        # The current KPL service may accept ``date`` but return a current
        # snapshot without echoing the effective date.  Keep those rows for
        # exploratory use, but explicitly mark them unverified so historical
        # backtests never mistake a snapshot for a dated observation.
        date_verified = _payload_date(payload) == _iso(trade_date)
        self.store.conn.execute("DELETE FROM kpl_concept_daily WHERE trade_date=?", [trade_date])
        self.store.conn.execute("DELETE FROM kpl_concept_stock_history WHERE trade_date=?", [trade_date])
        concept_rows = []
        member_rows = []
        for rank, sector in enumerate(sectors, 1):
            if not isinstance(sector, dict):
                continue
            code = str(sector.get("sector_code") or sector.get("code") or "")
            name = str(sector.get("sector_name") or sector.get("name") or "")
            if not code:
                continue
            stocks = sector.get("stocks") or []
            concept_rows.append((trade_date, code, name, rank, int(sector.get("stock_count") or len(stocks)),
                                 "kpl", _json({"requested_date": trade_date, "date_verified": date_verified, "payload": sector}),
                                 date_verified))
            for stock in stocks:
                if isinstance(stock, dict):
                    stock_code = str(stock.get("stock_code") or stock.get("code") or stock.get("股票代码") or "")
                    stock_name = str(stock.get("stock_name") or stock.get("name") or stock.get("股票名称") or "")
                elif isinstance(stock, (list, tuple)) and stock:
                    stock_code = str(stock[0])
                    stock_name = str(stock[1]) if len(stock) > 1 else ""
                else:
                    continue
                if stock_code:
                    member_rows.append((trade_date, code, name, stock_code, stock_name, rank, "kpl",
                                        _json({"requested_date": trade_date, "date_verified": date_verified, "payload": stock}),
                                        date_verified))
        count = self.store.insert_rows(
            "kpl_concept_daily", concept_rows,
            ["trade_date", "concept_code", "concept_name", "rank", "stock_count", "source", "raw_json", "date_verified"],
            replace_on=["trade_date", "concept_code"],
        )
        count += self.store.insert_rows(
            "kpl_concept_stock_history", member_rows,
            ["trade_date", "concept_code", "concept_name", "stock_code", "stock_name", "concept_rank", "source", "raw_json", "date_verified"],
            replace_on=["trade_date", "concept_code", "stock_code"],
        )
        self.store.conn.commit()
        return count

    def collect_full_membership_snapshot(self, snapshot_date: str) -> int:
        """Fetch current all-plate membership and label it only as snapshot_date."""
        payload = self.client.get("/sector/plates")
        plates = payload.get("plates", []) if isinstance(payload, dict) else []
        if not plates:
            return 0
        self.store.conn.execute("DELETE FROM kpl_concept_daily WHERE trade_date=?", [snapshot_date])
        self.store.conn.execute("DELETE FROM kpl_concept_stock_history WHERE trade_date=?", [snapshot_date])
        concepts = []
        members = []
        for rank, plate in enumerate(plates, 1):
            code = str(plate.get("code") or plate.get("plate_code") or "") if isinstance(plate, dict) else ""
            name = str(plate.get("name") or plate.get("plate_name") or "") if isinstance(plate, dict) else ""
            if not code:
                continue
            data = self.client.get("/sector/all-stocks", {"code": code})
            stocks = data if isinstance(data, list) else (data.get("stocks", data.get("data", [])) if isinstance(data, dict) else [])
            concepts.append((snapshot_date, code, name, rank, len(stocks or []), "kpl_full_snapshot", _json(plate), False))
            for stock in stocks or []:
                if isinstance(stock, dict):
                    stock_code = str(stock.get("stock_code") or stock.get("code") or "")
                    stock_name = str(stock.get("stock_name") or stock.get("name") or "")
                elif isinstance(stock, (list, tuple)) and stock:
                    stock_code = str(stock[0])
                    stock_name = str(stock[1]) if len(stock) > 1 else ""
                else:
                    continue
                if stock_code:
                    members.append((snapshot_date, code, name, stock_code, stock_name, rank, "kpl_full_snapshot", _json(stock), False))
        count = self.store.insert_rows("kpl_concept_daily", concepts,
                                       ["trade_date", "concept_code", "concept_name", "rank", "stock_count", "source", "raw_json", "date_verified"],
                                       replace_on=["trade_date", "concept_code"])
        count += self.store.insert_rows("kpl_concept_stock_history", members,
                                        ["trade_date", "concept_code", "concept_name", "stock_code", "stock_name", "concept_rank", "source", "raw_json", "date_verified"],
                                        replace_on=["trade_date", "concept_code", "stock_code"])
        self.store.conn.commit()
        return count

    def run(self, start_date: str, end_date: str, *, max_days: int | None = None,
            force: bool = False, mode: str = "ranking") -> dict[str, Any]:
        dates = _weekday_dates(start_date, end_date)
        if max_days:
            dates = dates[: max(0, int(max_days))]
        dataset = "kpl_concept_ranking" if mode == "ranking" else "kpl_concept_full_snapshot"
        results = []
        if mode == "full":
            dates = dates[-1:] if dates else []
        for trade_date in dates:
            if self._done(dataset, trade_date, force):
                results.append({"trade_date": trade_date, "status": "skipped"})
                continue
            self._checkpoint(dataset, trade_date, "running", attempts=1)
            try:
                rows = self.collect_ranking(trade_date) if mode == "ranking" else self.collect_full_membership_snapshot(trade_date)
                status = "success" if rows else "empty"
                self._checkpoint(dataset, trade_date, status, rows=rows, attempts=1)
                results.append({"trade_date": trade_date, "status": status, "rows": rows})
            except Exception as exc:
                self._checkpoint(dataset, trade_date, "error", attempts=1, error=str(exc))
                results.append({"trade_date": trade_date, "status": "error", "error": str(exc)[:240]})
        return {"start_date": _iso(start_date), "end_date": _iso(end_date), "dates": dates,
                "mode": mode, "results": results, "stats": self.client.stats}


def render_report(db_path: str | Path, result: dict[str, Any], out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        concept_count, member_count, verified_concepts, verified_members = con.execute(
            "SELECT (SELECT count(*) FROM kpl_concept_daily), (SELECT count(*) FROM kpl_concept_stock_history),"
            "(SELECT count(*) FROM kpl_concept_daily WHERE date_verified),"
            "(SELECT count(*) FROM kpl_concept_stock_history WHERE date_verified)"
        ).fetchone()
        latest = con.execute("SELECT max(trade_date) FROM kpl_concept_daily").fetchone()[0]
    finally:
        con.close()
    summary = {}
    for item in result["results"]:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    lines = ["# KPL 2026 概念与成分股回填", "", f"- mode: `{result['mode']}`",
             f"- date_range: `{result['start_date']}` ~ `{result['end_date']}`",
             f"- dates: {len(result['dates'])}", f"- summary: `{summary}`", f"- api_stats: `{result['stats']}`", "",
             "| table | rows | latest_date |", "|---|---:|---|",
             f"| kpl_concept_daily | {concept_count} | {latest or '-'} |",
             f"| kpl_concept_stock_history | {member_count} | {latest or '-'} |",
             "", f"- date_verified concept rows: {verified_concepts}",
             f"- date_verified member rows: {verified_members}",
             "- If date_verified is 0, the endpoint returned a snapshot without echoing the requested historical date; do not use those rows as a dated backtest feature."]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
