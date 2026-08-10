"""Bounded L2 stock-curve collection for operator candidates.

Production phase mode only runs ``fetch_all.py --only-market``, which never
reaches ``collect_all_l2``.  That froze ``l2_stock_intraday`` at an old date.
This module collects a **small candidate universe** every intraday/close tick
so stage signals can use L2 last prices as an executable-price fallback.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
from typing import Any

import duckdb

from base import DuckDBStore, KPLClient, logger
from collect_l2 import collect_l2_stock_bigorder, collect_l2_stock_intraday
from schema import init_schema
from trade_system.executable_quotes import candidate_codes_for_quotes
from trade_system.quality import table_exists


def _compact_date(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _hhmm(value: str) -> str:
    text = str(value or "")
    if " " in text:
        text = text.split(" ")[-1]
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text


def _payload_has_intraday_points(data) -> bool:
    if not data:
        return False
    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict):
        for key in ("data", "intraday", "list", "items"):
            val = data.get(key)
            if isinstance(val, list) and val:
                return True
        # Some APIs nest points under raw_data / points.
        for key in ("points", "raw_data"):
            val = data.get(key)
            if isinstance(val, list) and val:
                return True
    return False


def _probe_kpl_stock_intraday(
    trade_date: str,
    sample_code: str,
    *,
    total_budget_seconds: float = 20.0,
) -> bool:
    """One-shot probe: True only when KPL returns non-empty same-date points.

    Uses max_attempts=1 so an empty response does not burn 20–40s of backoff
    before the Eastmoney trends2 fallback runs.
    """
    probe = KPLClient(
        request_timeout=12,
        max_attempts=1,
        total_budget_seconds=total_budget_seconds,
    )
    data = probe.get(
        "/l2/stock-intraday",
        {"code": sample_code, "date": trade_date},
    )
    ok = _payload_has_intraday_points(data)
    if not ok:
        logger.info(
            "L2 focus: KPL /l2/stock-intraday probe empty for %s on %s; "
            "skipping KPL loop → Eastmoney trends2",
            sample_code,
            trade_date,
        )
    return ok


def _write_eastmoney_trends_fallback(
    store: DuckDBStore,
    trade_date: str,
    codes: list[str],
    *,
    deadline: float | None = None,
) -> dict[str, int]:
    """When KPL /l2/stock-intraday is empty, fill minute curves from Eastmoney trends2."""
    from trade_system.stock_data_sources import _from_em_trends

    ymd = "".join(ch for ch in trade_date if ch.isdigit())[:8]
    rows_written = 0
    codes_ok = 0
    for code in codes:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            payload = _from_em_trends(code, ymd)
        except Exception:
            payload = None
        if not payload or not payload.get("trends"):
            continue
        rows = []
        for pt in payload["trends"]:
            if not isinstance(pt, dict):
                continue
            if _compact_date(pt.get("time")) != ymd:
                continue
            price = pt.get("price")
            if price is None:
                continue
            rows.append(
                (
                    trade_date,
                    code,
                    _hhmm(pt.get("time")),
                    price,
                    pt.get("avg") or 0,
                    int(pt.get("volume") or 0),
                    int(pt.get("amount") or 0),
                    0,
                )
            )
        if not rows:
            continue
        n = store.insert_rows(
            "l2_stock_intraday",
            rows,
            [
                "date",
                "stock_code",
                "time",
                "price",
                "avg_price",
                "volume",
                "turnover",
                "main_fund_net",
            ],
        )
        if n:
            rows_written += n
            codes_ok += 1
            try:
                store.log_collect(
                    "l2_stock_intraday",
                    "eastmoney_trends2",
                    n,
                    "ok",
                )
            except Exception:
                pass
    return {"intraday_rows": rows_written, "stock_codes_ok": codes_ok}


def ensure_l2_focus_checkpoint(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS l2_focus_batch (
            trade_date DATE PRIMARY KEY,
            run_id VARCHAR,
            requested_stocks INTEGER,
            intraday_rows INTEGER,
            bigorder_rows INTEGER,
            stock_codes_ok INTEGER,
            status VARCHAR,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )


def _new_staging_store() -> DuckDBStore:
    store = DuckDBStore(":memory:")
    store.conn.execute(
        """
        CREATE TABLE l2_stock_intraday(
            date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE,
            avg_price DOUBLE, volume BIGINT, turnover BIGINT,
            main_fund_net BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    store.conn.execute(
        """
        CREATE TABLE l2_stock_bigorder(
            date DATE, stock_code VARCHAR, time VARCHAR,
            big_net_amount BIGINT, big_buy BIGINT, big_sell BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    return store


def _publish_staged_l2(
    target: DuckDBStore,
    staging: DuckDBStore,
    trade_date: str,
    *,
    include_bigorder: bool,
) -> tuple[int, int]:
    """Atomically replace only code/date keys present in the staged batch."""
    specs = [
        (
            "l2_stock_intraday",
            [
                "date", "stock_code", "time", "price", "avg_price",
                "volume", "turnover", "main_fund_net",
            ],
        )
    ]
    if include_bigorder:
        specs.append(
            (
                "l2_stock_bigorder",
                [
                    "date", "stock_code", "time",
                    "big_net_amount", "big_buy", "big_sell",
                ],
            )
        )
    prepared: list[tuple[str, str, list[str], list[tuple]]] = []
    for table, columns in specs:
        projection = ", ".join(columns)
        rows = staging.conn.execute(
            f"SELECT {projection} FROM {table} "
            "WHERE CAST(date AS VARCHAR)=?",
            [trade_date],
        ).fetchall()
        if not rows:
            continue
        temp = f"_stage_{table}_{datetime.now().strftime('%H%M%S%f')}"
        target.conn.execute(
            f"CREATE TEMP TABLE {temp} AS "
            f"SELECT {projection} FROM {table} LIMIT 0"
        )
        placeholders = ",".join("?" for _ in columns)
        target.conn.executemany(
            f"INSERT INTO {temp}({projection}) VALUES ({placeholders})",
            rows,
        )
        prepared.append((table, temp, columns, rows))
    if not prepared:
        return 0, 0
    try:
        target.conn.execute("BEGIN TRANSACTION")
        for table, temp, columns, _rows in prepared:
            projection = ", ".join(columns)
            target.conn.execute(
                f"DELETE FROM {table} AS old USING "
                f"(SELECT DISTINCT date, stock_code FROM {temp}) AS fresh "
                "WHERE old.date=fresh.date AND old.stock_code=fresh.stock_code"
            )
            target.conn.execute(
                f"INSERT INTO {table}({projection}) "
                f"SELECT {projection} FROM {temp}"
            )
        target.conn.execute("COMMIT")
    except Exception:
        target.conn.execute("ROLLBACK")
        raise
    finally:
        for _table, temp, _columns, _rows in prepared:
            target.conn.execute(f"DROP TABLE IF EXISTS {temp}")
    counts = {table: len(rows) for table, _temp, _columns, rows in prepared}
    return counts.get("l2_stock_intraday", 0), counts.get("l2_stock_bigorder", 0)


def collect_l2_focus(
    db_path: str | Path,
    trade_date: str,
    *,
    max_stocks: int = 40,
    codes: list[str] | None = None,
    total_budget_seconds: float = 90.0,
    include_bigorder: bool = True,
    client: KPLClient | None = None,
    prefer_eastmoney_trends: bool = False,
    skip_kpl_probe: bool = False,
) -> dict[str, Any]:
    """Collect L2 stock-intraday (and optional bigorder) for candidate codes.

    ``prefer_eastmoney_trends`` / failed KPL probe skips the multi-stock KPL
    loop entirely (avoids empty-endpoint cooldown storms).
    """
    run_id = f"l2_focus_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        store = DuckDBStore(str(db_path))
    except Exception as exc:
        return {
            "trade_date": trade_date,
            "run_id": run_id,
            "requested_stocks": 0,
            "intraday_rows": 0,
            "bigorder_rows": 0,
            "stock_codes_ok": 0,
            "status": "error",
            "error": f"db_locked_or_unavailable: {exc}"[:500],
            "codes": [],
        }
    client = client or KPLClient(
        request_timeout=15,
        max_attempts=2,
        total_budget_seconds=total_budget_seconds,
    )
    result: dict[str, Any] = {
        "trade_date": trade_date,
        "run_id": run_id,
        "requested_stocks": 0,
        "intraday_rows": 0,
        "bigorder_rows": 0,
        "stock_codes_ok": 0,
        "status": "running",
        "error": "",
        "codes": [],
    }
    deadline = time.monotonic() + max(1.0, float(total_budget_seconds))
    staging: DuckDBStore | None = None
    try:
        init_schema(store.conn)
        ensure_l2_focus_checkpoint(store.conn)
        targets = codes or candidate_codes_for_quotes(
            store.conn, trade_date, limit=max_stocks
        )
        targets = [str(c).zfill(6) if str(c).isdigit() else str(c) for c in targets]
        targets = targets[: max(1, int(max_stocks))]
        result["codes"] = targets
        result["requested_stocks"] = len(targets)
        if not targets:
            result["status"] = "empty_universe"
            store.conn.execute(
                "INSERT INTO l2_focus_batch("
                "trade_date,run_id,requested_stocks,intraday_rows,bigorder_rows,"
                "stock_codes_ok,status,last_error,updated_at) "
                "VALUES (CAST(? AS DATE),?,?,0,0,0,?,?,current_timestamp) "
                "ON CONFLICT(trade_date) DO UPDATE SET "
                "run_id=excluded.run_id,requested_stocks=excluded.requested_stocks,"
                "status=excluded.status,last_error=excluded.last_error,"
                "updated_at=excluded.updated_at",
                [trade_date, run_id, 0, "empty_universe", ""],
            )
            return result

        staging = _new_staging_store()

        source = "kpl_l2"
        intraday_rows = 0
        bigorder_rows = 0
        kpl_probe_ok = False
        if prefer_eastmoney_trends or skip_kpl_probe:
            kpl_probe_ok = False
            source = "eastmoney_trends2_direct"
        else:
            # One-shot probe: empty KPL must not iterate all candidates with
            # 20s/40s empty backoff each (was burning the whole phase budget).
            kpl_probe_ok = _probe_kpl_stock_intraday(
                trade_date,
                targets[0],
                total_budget_seconds=min(20.0, float(total_budget_seconds)),
            )

        if kpl_probe_ok:
            source = "kpl_l2"
            intraday_rows = collect_l2_stock_intraday(
                client, staging, trade_date, targets
            )
            if include_bigorder and getattr(client, "_circuit_open_reason", None) is None:
                bigorder_rows = collect_l2_stock_bigorder(
                    client, staging, trade_date, targets
                )
        else:
            fb = _write_eastmoney_trends_fallback(
                staging, trade_date, targets, deadline=deadline
            )
            source = "eastmoney_trends2"
            intraday_rows = int(fb.get("intraday_rows") or 0)
            # bigorder has no independent EM fallback in this module

        # Count distinct codes that actually landed same-date rows.
        codes_ok = 0
        try:
            codes_ok = int(
                staging.conn.execute(
                    "SELECT count(DISTINCT stock_code) FROM l2_stock_intraday "
                    "WHERE CAST(date AS VARCHAR)=? AND stock_code IN ("
                    + ",".join("?" for _ in targets)
                    + ")",
                    [trade_date, *targets],
                ).fetchone()[0]
                or 0
            )
        except Exception:
            codes_ok = 0

        # If probe said KPL was alive but landed nothing, still try EM.
        if (codes_ok == 0 or int(intraday_rows or 0) == 0) and kpl_probe_ok:
            fb = _write_eastmoney_trends_fallback(
                staging, trade_date, targets, deadline=deadline
            )
            if fb.get("stock_codes_ok", 0) > 0:
                source = "eastmoney_trends2"
                intraday_rows = int(fb.get("intraday_rows") or 0)
                codes_ok = int(fb.get("stock_codes_ok") or 0)

        # Fill only missing candidates from the independent trends source.
        landed = {
            str(row[0])
            for row in staging.conn.execute(
                "SELECT DISTINCT stock_code FROM l2_stock_intraday "
                "WHERE CAST(date AS VARCHAR)=?",
                [trade_date],
            ).fetchall()
        }
        missing_targets = [code for code in targets if code not in landed]
        if missing_targets and source == "kpl_l2":
            _write_eastmoney_trends_fallback(
                staging, trade_date, missing_targets, deadline=deadline
            )
            codes_ok = int(
                staging.conn.execute(
                    "SELECT count(DISTINCT stock_code) FROM l2_stock_intraday "
                    "WHERE CAST(date AS VARCHAR)=?",
                    [trade_date],
                ).fetchone()[0]
                or 0
            )

        intraday_rows, bigorder_rows = _publish_staged_l2(
            store,
            staging,
            trade_date,
            include_bigorder=include_bigorder,
        )

        coverage_pct = (
            round(100.0 * codes_ok / len(targets), 2) if targets else 0.0
        )
        status = (
            "success"
            if codes_ok > 0 and coverage_pct >= 99.5
            else "partial"
            if codes_ok > 0
            else "empty"
        )
        if (
            kpl_probe_ok
            and client.stats.get("circuit_open")
            and source == "kpl_l2"
        ):
            status = "circuit_open" if codes_ok == 0 else "partial_circuit"

        result.update(
            {
                "intraday_rows": int(intraday_rows or 0),
                "bigorder_rows": int(bigorder_rows or 0),
                "stock_codes_ok": codes_ok,
                "coverage_pct": coverage_pct,
                "status": status,
                "source": source,
                "kpl_probe_ok": kpl_probe_ok,
                "client_stats": dict(client.stats),
                "budget_exhausted": time.monotonic() >= deadline,
            }
        )
        store.conn.execute(
            "INSERT INTO l2_focus_batch("
            "trade_date,run_id,requested_stocks,intraday_rows,bigorder_rows,"
            "stock_codes_ok,status,last_error,updated_at) "
            "VALUES (CAST(? AS DATE),?,?,?,?,?,?,?,current_timestamp) "
            "ON CONFLICT(trade_date) DO UPDATE SET "
            "run_id=excluded.run_id,requested_stocks=excluded.requested_stocks,"
            "intraday_rows=excluded.intraday_rows,bigorder_rows=excluded.bigorder_rows,"
            "stock_codes_ok=excluded.stock_codes_ok,status=excluded.status,"
            "last_error=excluded.last_error,updated_at=excluded.updated_at",
            [
                trade_date,
                run_id,
                len(targets),
                int(intraday_rows or 0),
                int(bigorder_rows or 0),
                codes_ok,
                status,
                "",
            ],
        )
        return result
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:500]
        try:
            ensure_l2_focus_checkpoint(store.conn)
            store.conn.execute(
                "INSERT INTO l2_focus_batch("
                "trade_date,run_id,requested_stocks,intraday_rows,bigorder_rows,"
                "stock_codes_ok,status,last_error,updated_at) "
                "VALUES (CAST(? AS DATE),?,?,0,0,0,?,?,current_timestamp) "
                "ON CONFLICT(trade_date) DO UPDATE SET "
                "status=excluded.status,last_error=excluded.last_error,"
                "updated_at=excluded.updated_at",
                [trade_date, run_id, result.get("requested_stocks") or 0, "error", result["error"]],
            )
        except Exception:
            pass
        return result
    finally:
        if staging is not None:
            staging.close()
        store.close()
