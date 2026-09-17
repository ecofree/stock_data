"""Explicit xiaodefa collection and staging persistence, outside research runtime."""
from __future__ import annotations
import argparse
import os
import time
from datetime import datetime
from typing import Any
from base import DuckDBStore
from trade_system.limit_rules import limit_threshold
from trade_system.trading_calendar import previous_open_session
from trade_system.xiaodefa_source import XiaodefaClient, XiaodefaError
def _iso_date(value) -> str | None:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None

def _compact_date(value) -> str | None:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 8:
        return raw[:8]
    return None

def fetch_cyq_perf(client: XiaodefaClient, trade_date: str) -> list[dict[str, Any]]:
    day = _compact_date(trade_date)
    rows = client.query_all("cyq_perf", trade_date=day)
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_cyq_chips(
    client: XiaodefaClient, ts_code: str, trade_date: str
) -> list[dict[str, Any]]:
    rows = client.query_all(
        "cyq_chips", ts_code=ts_code, trade_date=_compact_date(trade_date)
    )
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_cyq_chips_for_args(
    client: XiaodefaClient, args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Fetch one bounded detailed chip distribution for an explicit stock."""
    code = str(getattr(args, "ts_code", "") or "").strip()
    if not code:
        raise XiaodefaError("chips requires --ts-code; refusing an unbounded all-stock fetch")
    return fetch_cyq_chips(client, code, args.trade_date)


def fetch_moneyflow_hsgt(
    client: XiaodefaClient, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    rows = client.query_all(
        "moneyflow_hsgt",
        start_date=_compact_date(start_date),
        end_date=_compact_date(end_date),
    )
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_ggt_daily(
    client: XiaodefaClient, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    rows = client.query_all(
        "ggt_daily",
        start_date=_compact_date(start_date),
        end_date=_compact_date(end_date),
    )
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_margin(client: XiaodefaClient, trade_date: str) -> list[dict[str, Any]]:
    rows = client.query_all("margin", trade_date=_compact_date(trade_date))
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_margin_detail(client: XiaodefaClient, trade_date: str) -> list[dict[str, Any]]:
    rows = client.query_all("margin_detail", trade_date=_compact_date(trade_date))
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_margin_detail_fallback(trade_date: str) -> list[dict[str, Any]]:
    """Recover all-stock margin detail from Eastmoney when the relay is late."""
    from trade_system.stock_data_sources import _from_em_margin_detail_daily

    rows = _from_em_margin_detail_daily(trade_date)
    out = []
    for row in rows or []:
        secucode = str(row.get("SECUCODE") or "").strip()
        code = str(row.get("SCODE") or "").strip()
        if "." in secucode:
            ts_code = secucode
        elif len(code) == 6 and code.isdigit():
            market = str(row.get("MARKET") or "").upper()
            suffix = ".SH" if (market == "" and code.startswith("6")) or "上交" in market else ".SZ"
            ts_code = f"{code}{suffix}"
        else:
            continue
        out.append(
            {
                "trade_date": _iso_date(row.get("DATE")) or trade_date,
                "ts_code": ts_code,
                "rzye": row.get("RZYE"),
                "rqye": row.get("RQYE"),
                "rzmre": row.get("RZMRE"),
                "rqyl": row.get("RQYL"),
                "rzche": row.get("RZCHE"),
                "rqchl": row.get("RQCHL"),
                "rqmcl": row.get("RQMCL"),
                "rzrqye": row.get("RZRQYE"),
                "provider": "eastmoney_datacenter",
            }
        )
    return [row for row in out if row["trade_date"] == trade_date]


def fetch_share_float(
    client: XiaodefaClient, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    rows = client.query_all(
        "share_float",
        start_date=_compact_date(start_date),
        end_date=_compact_date(end_date),
    )
    for row in rows:
        row["ann_date"] = _iso_date(row.get("ann_date"))
        row["float_date"] = _iso_date(row.get("float_date"))
    return rows


def fetch_stk_premarket(client: XiaodefaClient, trade_date: str) -> list[dict[str, Any]]:
    rows = client.query_all("stk_premarket", trade_date=_compact_date(trade_date))
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_kpl_list(client: XiaodefaClient, trade_date: str) -> list[dict[str, Any]]:
    rows = client.query_all("kpl_list", trade_date=_compact_date(trade_date))
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def fetch_hk_hold(client: XiaodefaClient, trade_date: str) -> list[dict[str, Any]]:
    rows = client.query_all("hk_hold", trade_date=_compact_date(trade_date))
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
    return [r for r in rows if r["trade_date"]]


def _enrich_limit_board_levels(store: DuckDBStore, rows: list[dict[str, Any]],
                               trade_date: str) -> None:
    """Derive consecutive limit-up board counts from local daily kline.

    TuShare limit_list_d carries no board level; without this the ladder and
    promotion-rate views would lose their height dimension on days where the
    KPL pool is unavailable.
    """
    if not rows:
        return
    codes = [str(r.get("ts_code") or "").split(".")[0] for r in rows]
    placeholders = ",".join("?" for _ in codes)
    try:
        hist = store.conn.execute(
            f"""
            SELECT regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS code,
                   CAST(trade_date AS DATE) AS d, CAST(change_pct AS DOUBLE) AS pct
            FROM v_kline_daily
            WHERE regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') IN ({placeholders})
              AND CAST(trade_date AS DATE) <= CAST(? AS DATE)
            ORDER BY code, d DESC
            """,
            [*codes, trade_date],
        ).fetchall()
    except Exception:
        return

    streak_by_code: dict[str, dict[str, float]] = {}
    for code, day, pct in hist:
        streak_by_code.setdefault(code, {})[str(day)[:10]] = pct or 0.0

    target = trade_date[:10]
    for row in rows:
        code = str(row.get("ts_code") or "").split(".")[0]
        days_map = streak_by_code.get(code)
        if not days_map:
            row["board_level"] = None
            continue
        streak = 0
        cursor = target
        from datetime import date as _date

        day = _date.fromisoformat(target)
        for _ in range(40):
            pct = days_map.get(cursor)
            if pct is None or pct < limit_threshold(code, row.get("name")):
                break
            streak += 1
            previous = previous_open_session(store.conn, cursor)
            if previous is None:
                break
            cursor = previous
        row["board_level"] = streak if streak > 0 else 1


def fetch_limit_pool(client: XiaodefaClient, trade_date: str) -> list[dict[str, Any]]:
    """Exchange-grade limit-up list (TuShare limit_list_d, limit_type=U)."""
    rows = client.query_all(
        "limit_list_d", trade_date=_compact_date(trade_date), limit_type="U"
    )
    for row in rows:
        row["trade_date"] = _iso_date(row.get("trade_date"))
        # `limit` is a reserved word in DuckDB DDL; rename before storage.
        if "limit" in row:
            row["limit_type"] = row.pop("limit")
        row["fetched_at"] = datetime.now()
    return [r for r in rows if r["trade_date"]]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _columns_of(rows: list[dict[str, Any]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows[:50]:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def store_rows(
    store: DuckDBStore,
    table_name: str,
    rows: list[dict[str, Any]],
    replace_on: tuple[str, ...],
    *,
    provider: str = "xiaodefa",
) -> int:
    if not rows:
        return 0
    columns = _columns_of(rows)
    payload = [
        tuple(row.get(col) for col in columns)
        for row in rows
    ]
    store.insert_rows(table_name, payload, columns, replace_on=list(replace_on))
    store.execute(
        "INSERT INTO _collect_log (table_name, endpoint, rows_inserted, status) "
        "VALUES (?, ?, ?, ?)",
        [table_name, provider, len(payload), "ok"],
    )
    return len(payload)


COLLECTORS: dict[str, dict[str, Any]] = {
    "cyq": {
        "table": "xdf_cyq_perf",
        "replace_on": ("trade_date", "ts_code"),
        "fetch": lambda c, args: fetch_cyq_perf(c, args.trade_date),
        "empty_is_error": True,
        "min_rows": 1000,
    },
    "chips": {
        "table": "xdf_cyq_chips",
        "replace_on": ("trade_date", "ts_code", "price"),
        "fetch": lambda c, args: fetch_cyq_chips_for_args(c, args),
        "empty_is_error": True,
        "min_rows": 1,
    },
    "hsgt": {
        "table": "xdf_moneyflow_hsgt",
        "replace_on": ("trade_date",),
        "fetch": lambda c, args: fetch_moneyflow_hsgt(c, args.start_date, args.end_date),
    },
    "ggt": {
        "table": "xdf_ggt_daily",
        "replace_on": ("trade_date",),
        "fetch": lambda c, args: fetch_ggt_daily(c, args.start_date, args.end_date),
    },
    "margin": {
        "table": "xdf_margin_summary",
        "replace_on": ("trade_date", "exchange_id"),
        "fetch": lambda c, args: fetch_margin(c, args.trade_date),
        "empty_is_error": True,
        # A single exchange is not a Shanghai+Shenzhen margin balance and
        # must not be presented as the market aggregate.
        "min_rows": 2,
    },
    "margin_detail": {
        "table": "xdf_margin_detail",
        "replace_on": ("trade_date", "ts_code"),
        "fetch": lambda c, args: fetch_margin_detail(c, args.trade_date),
        "empty_is_error": True,
        "min_rows": 1000,
    },
    "float": {
        "table": "xdf_share_float",
        "replace_on": ("ts_code", "float_date", "share_type", "holder_name"),
        "fetch": lambda c, args: fetch_share_float(c, args.start_date, args.end_date),
    },
    "premarket": {
        "table": "xdf_stk_premarket",
        "replace_on": ("trade_date", "ts_code"),
        "fetch": lambda c, args: fetch_stk_premarket(c, args.trade_date),
    },
    "kpl": {
        "table": "xdf_kpl_list",
        "replace_on": ("trade_date", "ts_code"),
        "fetch": lambda c, args: fetch_kpl_list(c, args.trade_date),
    },
    "hk_hold": {
        "table": "xdf_hk_hold",
        "replace_on": ("trade_date", "ts_code"),
        "fetch": lambda c, args: fetch_hk_hold(c, args.trade_date),
    },
    "limit_pool": {
        "table": "xdf_limit_pool",
        "replace_on": ("trade_date", "ts_code"),
        "fetch": lambda c, args: fetch_limit_pool(c, args.trade_date),
    },
}


def collect(
    kinds: list[str],
    *,
    db_path: str | None = None,
    trade_date: str,
    start_date: str,
    end_date: str,
    ts_code: str | None = None,
) -> dict[str, Any]:
    client = XiaodefaClient()
    from base import DuckDBStore
    store = DuckDBStore(db_path)
    results: dict[str, Any] = {}
    try:
        for kind in kinds:
            spec = COLLECTORS[kind]
            started = time.time()
            try:
                rows = spec["fetch"](client, argparse.Namespace(
                    trade_date=trade_date,
                    start_date=start_date,
                    end_date=end_date,
                    ts_code=ts_code,
                ))
                fallback_used = False
                if kind == "margin_detail" and len(rows) < int(spec.get("min_rows") or 1):
                    fallback_rows = fetch_margin_detail_fallback(trade_date)
                    if len(fallback_rows) >= int(spec.get("min_rows") or 1):
                        rows = fallback_rows
                        fallback_used = True
                source_provider = "eastmoney_datacenter" if fallback_used else "xiaodefa"
                if kind == "margin_detail":
                    if os.environ.get("KPL_RUNTIME_SCHEMA_READY", "").strip() != "1":
                        store.execute(
                            "ALTER TABLE xdf_margin_detail "
                            "ADD COLUMN IF NOT EXISTS provider VARCHAR"
                        )
                    for row in rows:
                        row["provider"] = source_provider
                if spec.get("empty_is_error") and len(rows) < int(spec.get("min_rows") or 1):
                    raise XiaodefaError(
                        f"{kind} returned only {len(rows)} rows for "
                        f"trade_date={trade_date}; source may be late or unavailable"
                    )
                if kind == "limit_pool":
                    _enrich_limit_board_levels(store, rows, trade_date)
                    for r in rows:
                        r.setdefault("board_level")
                stored = store_rows(
                    store,
                    spec["table"],
                    rows,
                    spec["replace_on"],
                    provider=source_provider,
                )
                results[kind] = {
                    "status": "ok",
                    "table": spec["table"],
                    "rows": stored,
                    "provider": source_provider,
                    "elapsed_s": round(time.time() - started, 1),
                }
            except Exception as exc:
                results[kind] = {
                    "status": "error",
                    "table": spec["table"],
                    "message": str(exc)[:200],
                    "elapsed_s": round(time.time() - started, 1),
                }
    finally:
        store.conn.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="xiaodefa relay collector")
    parser.add_argument("--db", default=None, help="DuckDB path override")
    parser.add_argument("--trade-date", required=True, help="ISO date, e.g. 2026-08-21")
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--ts-code", default=None)
    parser.add_argument(
        "--kinds",
        default="cyq,hsgt,ggt,margin,margin_detail,float",
        help="comma list from: " + ",".join(COLLECTORS) + "; chips requires --ts-code",
    )
    args = parser.parse_args()

    start = args.start_date or args.trade_date
    end = args.end_date or args.trade_date
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in COLLECTORS]
    if unknown:
        parser.error(f"unknown kinds: {unknown}")

    outcomes = collect(
        kinds,
        db_path=args.db,
        trade_date=args.trade_date,
        start_date=start,
        end_date=end,
        ts_code=args.ts_code,
    )
    failed = False
    for kind, info in outcomes.items():
        mark = "OK " if info["status"] == "ok" else "ERR"
        detail = (
            f"rows={info['rows']}"
            if info["status"] == "ok"
            else f"msg={info['message']}"
        )
        print(f"[{mark}] {kind:14s} -> {info['table']} ({detail}, {info['elapsed_s']}s)")
        if info["status"] != "ok":
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
