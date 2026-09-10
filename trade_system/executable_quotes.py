"""Bounded live executable quotes for candidate stocks.

Full-market Eastmoney clist often falls back to the delayed host during a live
session.  When that happens, every stock is tagged
``eastmoney_intraday_clist_delay`` and the strict tradability gate correctly
refuses entry.  This module fills a **candidate-only** live quote table from
Tencent ``qt.gtimg.cn`` (no token) so stage signals can attach a same-session
reference price without waiting for a full-market live clist recovery.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable

import duckdb

from trade_system.quality import table_exists

# Provider tag used in executable_quote_snapshot / stage evidence.
TENCENT_SPOT_PROVIDER = "tencent_spot_quote"

# Common qt.gtimg.cn field indices (price confirmed in stock_data_sources).
_Q = {
    "name": 1,
    "code": 2,
    "price": 3,
    "pre_close": 4,
    "open": 5,
    "bid1": 9,
    "bid1_vol": 10,
    "ask1": 19,
    "ask1_vol": 20,
    "time": 30,
    "change_pct": 32,
}


def ensure_executable_quote_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS executable_quote_snapshot (
            trade_date DATE,
            stock_code VARCHAR,
            price DOUBLE,
            pre_close DOUBLE,
            bid1 DOUBLE,
            ask1 DOUBLE,
            bid1_vol DOUBLE,
            ask1_vol DOUBLE,
            change_pct DOUBLE,
            provider VARCHAR,
            quote_time VARCHAR,
            fetched_at TIMESTAMP,
            raw_json VARCHAR
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_executable_quote "
        "ON executable_quote_snapshot(trade_date, stock_code, provider)"
    )


def is_delayed_provider(provider: str | None) -> bool:
    text = str(provider or "").strip().lower()
    return "delay" in text or text.endswith("_delay")


def quote_trade_date(quote_time: Any) -> str | None:
    """Return ``YYYY-MM-DD`` only when the provider timestamp carries a date."""
    digits = "".join(ch for ch in str(quote_time or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _num(parts: list[str], index: int) -> float | None:
    try:
        raw = parts[index]
        if raw in (None, "", "-"):
            return None
        return float(raw)
    except (IndexError, TypeError, ValueError):
        return None


def parse_tencent_parts(parts: list[str]) -> dict[str, Any] | None:
    if not parts or len(parts) < 33:
        return None
    code = str(parts[_Q["code"]] if len(parts) > _Q["code"] else "").strip()
    price = _num(parts, _Q["price"])
    if not code or price is None or price <= 0:
        return None
    bid1 = _num(parts, _Q["bid1"])
    ask1 = _num(parts, _Q["ask1"])
    bid1_vol = _num(parts, _Q["bid1_vol"])
    ask1_vol = _num(parts, _Q["ask1_vol"])
    # Keep an empty book explicit.  The strategy layer uses it to distinguish
    # a quoted stock from one that can actually accept a buy order.
    if ask1 is not None and ask1 <= 0:
        ask1 = None
        ask1_vol = None
    if bid1 is not None and bid1 <= 0:
        bid1 = None
        bid1_vol = None
    return {
        "stock_code": code,
        "name": parts[_Q["name"]] if len(parts) > _Q["name"] else "",
        "price": price,
        "pre_close": _num(parts, _Q["pre_close"]),
        "open": _num(parts, _Q["open"]),
        "bid1": bid1,
        "ask1": ask1,
        "bid1_vol": bid1_vol,
        "ask1_vol": ask1_vol,
        "change_pct": _num(parts, _Q["change_pct"]),
        "quote_time": parts[_Q["time"]] if len(parts) > _Q["time"] else "",
        "provider": TENCENT_SPOT_PROVIDER,
        "raw_parts": parts,
    }


def fetch_tencent_quotes(codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Network call: batch Tencent spot quotes. Returns code -> parsed dict."""
    from trade_system.stock_data_sources import _from_tencent_quote

    codes_list = [str(c).zfill(6) if str(c).isdigit() else str(c) for c in codes]
    codes_list = [c for c in codes_list if c]
    if not codes_list:
        return {}
    raw = _from_tencent_quote(codes_list) or {}
    out: dict[str, dict[str, Any]] = {}
    for code, parts in raw.items():
        parsed = parse_tencent_parts(list(parts))
        if parsed:
            out[str(code)] = parsed
    return out


def candidate_codes_for_quotes(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    *,
    limit: int = 200,
) -> list[str]:
    """Prefer same-day actionable universe: limit pool → stage → score."""
    queries = (
        """
        SELECT stock_code FROM v_limit_pool
        WHERE CAST(trade_date AS VARCHAR)=?
        ORDER BY board_level DESC NULLS LAST, stock_code
        LIMIT ?
        """,
        """
        SELECT DISTINCT stock_code FROM stock_candidate_stage_signal
        WHERE CAST(trade_date AS VARCHAR)=?
        ORDER BY stock_code
        LIMIT ?
        """,
        """
        SELECT stock_code FROM stock_candidate_score
        WHERE CAST(trade_date AS VARCHAR)=?
        ORDER BY score DESC NULLS LAST, stock_code
        LIMIT ?
        """,
        """
        SELECT DISTINCT stock_code FROM multi_source_stock_flow
        WHERE source_date=CAST(? AS DATE) AND main_net IS NOT NULL
          AND main_net > 0
        ORDER BY main_net DESC
        LIMIT ?
        """,
    )
    for sql in queries:
        try:
            rows = con.execute(sql, [trade_date, int(limit)]).fetchall()
        except Exception:
            continue
        codes = [str(r[0]) for r in rows if r and r[0]]
        if codes:
            return codes
    return []


def upsert_executable_quotes(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    quotes: dict[str, dict[str, Any]],
    *,
    fetched_at: datetime | None = None,
) -> int:
    ensure_executable_quote_schema(con)
    now = fetched_at or datetime.now()
    normalized_trade_date = str(trade_date)[:10]
    written = 0
    for code, q in quotes.items():
        price = q.get("price")
        if (
            price is None
            or quote_trade_date(q.get("quote_time")) != normalized_trade_date
        ):
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO executable_quote_snapshot (
                trade_date, stock_code, price, pre_close, bid1, ask1,
                bid1_vol, ask1_vol, change_pct, provider, quote_time,
                fetched_at, raw_json
            ) VALUES (
                CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                trade_date,
                str(code),
                price,
                q.get("pre_close"),
                q.get("bid1"),
                q.get("ask1"),
                q.get("bid1_vol"),
                q.get("ask1_vol"),
                q.get("change_pct"),
                str(q.get("provider") or TENCENT_SPOT_PROVIDER),
                str(q.get("quote_time") or ""),
                now,
                json.dumps(
                    {k: v for k, v in q.items() if k != "raw_parts"},
                    ensure_ascii=False,
                    default=str,
                ),
            ],
        )
        written += 1
    return written


def collect_executable_quotes(
    db_path: str | Path,
    trade_date: str,
    *,
    limit: int = 200,
    codes: list[str] | None = None,
    fetcher=None,
    auto_boost_if_kpl_stale: bool = False,
) -> dict[str, Any]:
    """Collect and persist live quotes for candidate stocks."""
    effective_limit = int(limit)
    boost_meta: dict[str, Any] = {"boost": False, "consecutive_stale": 0, "reason": ""}
    if auto_boost_if_kpl_stale:
        from trade_system.kpl_health import (
            boosted_quote_limit,
            should_boost_alternative_sources,
        )

        boost_meta = should_boost_alternative_sources(db_path, trade_date)
        if boost_meta.get("boost"):
            effective_limit = boosted_quote_limit(limit, boost=True)

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        ensure_executable_quote_schema(con)
        targets = codes or candidate_codes_for_quotes(
            con, trade_date, limit=effective_limit
        )
        if not targets:
            return {
                "trade_date": trade_date,
                "requested": 0,
                "written": 0,
                "provider": TENCENT_SPOT_PROVIDER,
                "status": "empty_universe",
                "kpl_boost": boost_meta.get("boost"),
                "kpl_stale": boost_meta.get("consecutive_stale"),
            }
        # Batch in chunks of 60 (URL length safety).
        fetch = fetcher or fetch_tencent_quotes
        merged: dict[str, dict[str, Any]] = {}
        chunk = 60
        for i in range(0, len(targets), chunk):
            batch = targets[i : i + chunk]
            try:
                merged.update(fetch(batch) or {})
            except Exception as exc:
                return {
                    "trade_date": trade_date,
                    "requested": len(targets),
                    "written": 0,
                    "provider": TENCENT_SPOT_PROVIDER,
                    "status": "error",
                    "error": str(exc)[:400],
                    "kpl_boost": boost_meta.get("boost"),
                    "kpl_stale": boost_meta.get("consecutive_stale"),
                }
        written = upsert_executable_quotes(con, trade_date, merged)
        rejected = max(0, len(merged) - written)
        return {
            "trade_date": trade_date,
            "requested": len(targets),
            "returned": len(merged),
            "written": written,
            "provider": TENCENT_SPOT_PROVIDER,
            "status": (
                "success"
                if written and not rejected
                else "partial_invalid_timestamp"
                if written
                else "invalid_or_empty_response"
            ),
            "rejected": rejected,
            "limit": effective_limit,
            "kpl_boost": boost_meta.get("boost"),
            "kpl_stale": boost_meta.get("consecutive_stale"),
            "kpl_boost_reason": boost_meta.get("reason"),
        }
    finally:
        con.close()


def load_executable_quote(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    stock_code: str,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    if not table_exists(con, "executable_quote_snapshot"):
        return None
    params: list[Any] = [trade_date, stock_code]
    as_of_clause = ""
    if as_of is not None:
        as_of_clause = "AND fetched_at <= ?"
        params.append(as_of)
    rows = con.execute(
        f"""
        SELECT stock_code, price, pre_close, bid1, ask1, bid1_vol, ask1_vol,
               change_pct, provider, quote_time, fetched_at
        FROM executable_quote_snapshot
        WHERE CAST(trade_date AS VARCHAR)=?
          AND stock_code=?
          AND substr(
                regexp_replace(coalesce(quote_time,''), '[^0-9]', '', 'g'),
                1,
                8
              )=replace(CAST(trade_date AS VARCHAR),'-','')
          {as_of_clause}
        ORDER BY
          CASE WHEN lower(coalesce(provider,'')) LIKE '%delay%' THEN 0 ELSE 1 END DESC,
          fetched_at DESC NULLS LAST
        LIMIT 1
        """,
        params,
    ).fetchall()
    if not rows:
        return None
    r = rows[0]
    return {
        "stock_code": r[0],
        "price": r[1],
        "pre_close": r[2],
        "bid1": r[3],
        "ask1": r[4],
        "bid1_vol": r[5],
        "ask1_vol": r[6],
        "change_pct": r[7],
        "provider": r[8],
        "quote_time": r[9],
        "fetched_at": r[10],
    }
