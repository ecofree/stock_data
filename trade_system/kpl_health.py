"""KPL health helpers: stale tracker → alternative-source boost decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_exists

# Matches collect_market.KPL_STALE_ALERT_THRESHOLD intent: after several
# consecutive same-date misses, prefer independent live quotes / L2 focus.
DEFAULT_STALE_BOOST_THRESHOLD = 3


def _open_read_only(db_path: str | Path, *, attempts: int = 5, delay: float = 0.4):
    """Open DuckDB read-only with brief retries (scheduler may hold the lock)."""
    import time

    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return duckdb.connect(str(db_path), read_only=True)
        except Exception as exc:
            last_exc = exc
            if i + 1 < attempts:
                time.sleep(delay * (i + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"cannot open {db_path}")


def kpl_consecutive_stale(
    db_path: str | Path | duckdb.DuckDBPyConnection,
    trade_date: str,
) -> int:
    owns = not isinstance(db_path, duckdb.DuckDBPyConnection)
    try:
        con = _open_read_only(db_path) if owns else db_path
    except Exception:
        return 0
    try:
        if not table_exists(con, "kpl_stale_tracker"):
            return 0
        row = con.execute(
            "SELECT consecutive_stale FROM kpl_stale_tracker "
            "WHERE CAST(date AS VARCHAR)=?",
            [str(trade_date)[:10]],
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0
    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass


def should_boost_alternative_sources(
    db_path: str | Path | duckdb.DuckDBPyConnection,
    trade_date: str,
    *,
    threshold: int = DEFAULT_STALE_BOOST_THRESHOLD,
) -> dict[str, Any]:
    """Return whether KPL same-date market context is stale enough to boost alts."""
    stale = kpl_consecutive_stale(db_path, trade_date)
    boost = stale >= max(1, int(threshold))
    return {
        "trade_date": str(trade_date)[:10],
        "consecutive_stale": stale,
        "threshold": int(threshold),
        "boost": boost,
        "reason": (
            f"kpl_stale consecutive={stale} >= {threshold}"
            if boost
            else f"kpl_stale consecutive={stale} < {threshold}"
        ),
    }


def boosted_quote_limit(base_limit: int, *, boost: bool) -> int:
    base = max(20, int(base_limit or 200))
    return min(500, base * 2) if boost else base


def boosted_l2_stock_limit(base_limit: int, *, boost: bool) -> int:
    base = max(10, int(base_limit or 40))
    return min(120, base * 2) if boost else base
