"""Build same-session market context from the persisted stock-flow snapshot.

KPL is the preferred market-context provider, but its breadth endpoint can
remain on the previous session during trading.  A fresh, same-date Eastmoney
full-market batch that passes the 99.5% coverage contract is classified as
``secondary_verified``.  Lower coverage remains an explicit fallback.  Neither
form suppresses later KPL retries or overwrites a same-date KPL snapshot.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base import connect_duckdb  # noqa: E402
from schema import init_schema  # noqa: E402
from trade_system.normalize import build_normalized_views  # noqa: E402


def _same_date_exists(con: duckdb.DuckDBPyConnection, table: str, trade_date: str) -> bool:
    return bool(con.execute(
        f"SELECT 1 FROM \"{table}\" WHERE CAST(date AS VARCHAR)=? LIMIT 1",
        [trade_date],
    ).fetchone())


def _same_date_source_kind(
    con: duckdb.DuckDBPyConnection, table: str, trade_date: str
) -> str | None:
    if "source_kind" not in _table_columns(con, table):
        return "real" if _same_date_exists(con, table, trade_date) else None
    row = con.execute(
        f'SELECT source_kind FROM "{table}" WHERE CAST(date AS VARCHAR)=? '
        "ORDER BY source_kind NULLS LAST LIMIT 1",
        [trade_date],
    ).fetchone()
    return str(row[0] or "real").lower() if row else None


def _table_columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _limit_pct(con: duckdb.DuckDBPyConnection, stock_code: str) -> float:
    """Return the board/ST price-limit rule used for breadth counting."""
    code = "".join(ch for ch in str(stock_code or "") if ch.isdigit())[-6:]
    name = ""
    if code and _table_columns(con, "tushare_stock_basic"):
        row = con.execute(
            "SELECT stock_name FROM tushare_stock_basic WHERE stock_code=? LIMIT 1",
            [code],
        ).fetchone()
        name = str(row[0] or "") if row else ""
    if "ST" in name.upper().replace("*", ""):
        return 5.0
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("4", "8")):
        return 30.0
    return 10.0


def derive_market_context(
    db_path: str | Path,
    trade_date: str,
    *,
    now: datetime | None = None,
) -> dict:
    reference_now = now or datetime.now()
    con = connect_duckdb(str(db_path))
    try:
        init_schema(con)
        rows = con.execute(
            """
            SELECT stock_code, change_pct, turnover, close, provider
            FROM multi_source_stock_flow
            WHERE source_date = CAST(? AS DATE)
              AND NOT coalesce(is_stale, false)
              AND length(regexp_replace(CAST(stock_code AS VARCHAR), '[^0-9]', '', 'g'))=6
            """,
            [trade_date],
        ).fetchall()
        if not rows:
            return {
                "trade_date": trade_date,
                "status": "no_same_date_stock_flow",
                "rows": 0,
                "written": 0,
            }

        try:
            batch = con.execute(
                """
                SELECT coverage_pct,status,provider,updated_at,
                       expected_rows,fetched_rows
                FROM intraday_stock_flow_batch
                WHERE CAST(trade_date AS VARCHAR)=?
                """,
                [trade_date],
            ).fetchone()
        except duckdb.CatalogException:
            batch = None
        batch_coverage = float(batch[0] or 0) if batch else 0.0
        batch_status = str(batch[1] or "") if batch else ""
        batch_provider = str(batch[2] or "") if batch else ""
        batch_updated_at = batch[3] if batch else None
        if isinstance(batch_updated_at, str):
            try:
                batch_updated_at = datetime.fromisoformat(batch_updated_at)
            except ValueError:
                batch_updated_at = None
        batch_age_seconds = (
            max(0.0, (reference_now - batch_updated_at).total_seconds())
            if isinstance(batch_updated_at, datetime)
            else None
        )
        expected_rows = int(batch[4] or 0) if batch else 0
        fetched_rows = int(batch[5] or 0) if batch else 0
        coverage_codes = len({str(row[0]) for row in rows})
        verified_current = bool(
            batch
            and batch_coverage >= 99.5
            and batch_status in {"success", "success_with_unavailable"}
            and batch_provider.startswith("eastmoney_intraday_clist")
            and batch_age_seconds is not None
            and batch_age_seconds <= 900
            and batch_updated_at.date().isoformat() == trade_date
            and expected_rows > 0
            and fetched_rows > 0
            and coverage_codes >= min(
                fetched_rows, int(expected_rows * 0.995)
            )
        )
        source_kind = "secondary_verified" if verified_current else "fallback"

        changes = []
        turnovers = []
        providers: dict[str, int] = {}
        for _code, change, turnover, _close, provider in rows:
            try:
                pct = float(change) if change is not None else None
            except (TypeError, ValueError):
                pct = None
            if pct is not None:
                changes.append(pct)
            try:
                amount = float(turnover) if turnover is not None else None
            except (TypeError, ValueError):
                amount = None
            if amount is not None:
                turnovers.append(amount)
            providers[str(provider or "unknown")] = providers.get(str(provider or "unknown"), 0) + 1

        rise = sum(1 for value in changes if value > 0)
        fall = sum(1 for value in changes if value < 0)
        flat = sum(1 for value in changes if value == 0)
        limit_up = 0
        limit_down = 0
        limit_rules: dict[str, int] = {}
        for row in rows:
            try:
                change = float(row[1])
            except (TypeError, ValueError):
                continue
            rule = _limit_pct(con, str(row[0]))
            limit_rules[str(rule)] = limit_rules.get(str(rule), 0) + 1
            if change >= rule - 0.15:
                limit_up += 1
            if change <= -rule + 0.15:
                limit_down += 1
        coverage = coverage_codes
        payload = {
            "source": "derived_from_multi_source_stock_flow",
            "source_kind": source_kind,
            "fallback": not verified_current,
            "trade_date": trade_date,
            "coverage_codes": coverage,
            "batch_coverage_pct": batch_coverage,
            "batch_status": batch_status,
            "batch_provider": batch_provider,
            "batch_age_seconds": batch_age_seconds,
            "rise_count": rise,
            "fall_count": fall,
            "flat_count": flat,
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "turnover_sum": sum(turnovers) if turnovers else None,
            "providers": providers,
            "limit_rules": limit_rules,
            "generated_at": reference_now.isoformat(timespec="seconds"),
        }
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        written = 0

        # Never overwrite a same-date KPL/real provider snapshot.  A prior
        # fallback may be promoted after a later retry reaches 99.5%.
        mood_source = _same_date_source_kind(con, "market_mood", trade_date)
        if mood_source in {"fallback", "derived_current", "secondary_verified"}:
            con.execute(
                """
                UPDATE market_mood SET
                  rise_count=?,fall_count=?,limit_up_count=?,limit_down_count=?,
                  total_float=?,prev_float=NULL,rise_fall_ratio=?,market_color=?,
                  fetched_at=?,raw_json=?,source_kind=?
                WHERE CAST(date AS VARCHAR)=?
                """,
                [
                    rise, fall, limit_up, limit_down,
                    int(sum(turnovers)) if turnovers else None,
                    (rise / fall) if fall else None,
                    source_kind, reference_now, raw_json, source_kind, trade_date,
                ],
            )
            written += 1
        elif mood_source is None:
            mood_values = {
                "date": trade_date,
                "rise_count": rise,
                "fall_count": fall,
                "limit_up_count": limit_up,
                "limit_down_count": limit_down,
                "total_float": int(sum(turnovers)) if turnovers else None,
                "prev_float": None,
                "rise_fall_ratio": (rise / fall) if fall else None,
                "market_color": source_kind,
                "fetched_at": reference_now,
                "raw_json": raw_json,
                "source_kind": source_kind,
            }
            cols = _table_columns(con, "market_mood")
            selected = [key for key in mood_values if key in cols]
            con.execute(
                f"INSERT INTO market_mood ({','.join(selected)}) VALUES ({','.join('?' for _ in selected)})",
                [mood_values[key] for key in selected],
            )
            written += 1

        rise_fall_source = _same_date_source_kind(con, "market_rise_fall", trade_date)
        if rise_fall_source in {"fallback", "derived_current", "secondary_verified"}:
            con.execute(
                """
                UPDATE market_rise_fall SET
                  limit_up_count=?,limit_down_count=?,broken_limit_up_count=NULL,
                  blown_limit_up_count=NULL,blown_limit_up_rate=NULL,raw_field_5=?,
                  raw_json=?,updated_at=?,source_kind=?
                WHERE CAST(date AS VARCHAR)=?
                """,
                [
                    limit_up, limit_down, flat, raw_json, reference_now,
                    source_kind, trade_date,
                ],
            )
            written += 1
        elif rise_fall_source is None:
            con.execute(
                """
                INSERT INTO market_rise_fall
                (date,limit_up_count,limit_down_count,broken_limit_up_count,
                 blown_limit_up_count,blown_limit_up_rate,raw_field_5,raw_json,updated_at,source_kind)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [trade_date, limit_up, limit_down, None, None, None, flat,
                 raw_json, reference_now, source_kind],
            )
            written += 1

        summary_source = _same_date_source_kind(con, "daily_summary", trade_date)
        if summary_source in {"fallback", "derived_current", "secondary_verified"}:
            con.execute(
                """
                UPDATE daily_summary SET
                  limit_up_count=?,limit_down_count=?,rise_count=?,fall_count=?,
                  consecutive_count=NULL,raw_json=?,fetched_at=?,source_kind=?
                WHERE CAST(date AS VARCHAR)=?
                """,
                [
                    limit_up, limit_down, rise, fall, raw_json, reference_now,
                    source_kind, trade_date,
                ],
            )
            written += 1
        elif summary_source is None:
            con.execute(
                """
                INSERT INTO daily_summary
                (date,limit_up_count,limit_down_count,rise_count,fall_count,
                 consecutive_count,raw_json,fetched_at,source_kind)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    trade_date, limit_up, limit_down, rise, fall, None,
                    raw_json, reference_now, source_kind,
                ],
            )
            written += 1

        con.commit()
    finally:
        con.close()

    if written:
        build_normalized_views(db_path)
    return {
        "trade_date": trade_date,
        "status": (
            f"{source_kind}_written" if written else "same_date_snapshot_exists"
        ),
        "rows": len(rows),
        "coverage_codes": coverage,
        "coverage_pct": batch_coverage,
        "source_kind": source_kind,
        "written": written,
        "providers": providers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive market context from same-date stock flow.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", default="reports/market_context_latest.json")
    args = parser.parse_args()
    result = derive_market_context(args.db, args.date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()), f"report={out}")
    return 0 if result["status"] in {
        "fallback_written", "secondary_verified_written", "same_date_snapshot_exists",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
