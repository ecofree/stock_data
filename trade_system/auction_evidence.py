"""Auction evidence chain when real auction tick data is unavailable."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_columns, table_exists
from trade_system.db_utils import fetch_dicts as _fetch_dicts


EVIDENCE_COLUMNS = [
    "trade_date",
    "stock_code",
    "source_table",
    "confirmation",
    "auction_strength",
    "auction_amount",
    "tick_rows",
    "is_fallback",
    "missing_reason",
    "evidence_json",
]


def observed_auction_rows(payload, trade_date, provider, received_at, payload_hash):
    """Native final snapshots and historical opening bars never qualify as ticks."""
    from trade_system.units import _number
    final = provider == 'hithink'
    if final:
        from trade_system.hithink_client import CST
        timestamp = _number(payload.get('timestamp'))
        if (timestamp is None or datetime.fromtimestamp(timestamp/1000, CST).date().isoformat() != trade_date
                or received_at.date().isoformat() != trade_date
                or abs(received_at.replace(tzinfo=CST).timestamp()-timestamp/1000)>60
                or payload.get('data_status') != 'final' or payload.get('auction_phase') != 'closed'):
            raise ValueError('undated or unfinished native final auction snapshot')
        items = payload.get('item')
        if type(payload.get('total')) is not int or not isinstance(items,list) or len(items) != payload['total'] or len(items)>100:
            raise ValueError('incomplete scoped final snapshot')
    else:
        if (provider != 'xiaodefa' or payload.get('api') != 'stk_auction_o'
                or payload.get('params',{}).get('trade_date') != trade_date.replace('-','')):
            raise ValueError('wrong historical opening product or request date')
        items = payload.get('rows')
        if not isinstance(items,list) or len(items)>=10000:
            raise ValueError('opening snapshot page completeness unknown')
    output, seen = [], set()
    for item in items:
        ts_code = item.get('thscode' if final else 'ts_code', '')
        if (not isinstance(ts_code,str) or len(ts_code)!=9 or not ts_code[:6].isascii()
                or not ts_code[:6].isdigit() or ts_code[-3:] not in ('.SH','.SZ','.BJ') or ts_code in seen
                or (not final and item.get('trade_date') != trade_date.replace('-',''))):
            raise ValueError('invalid, repeated or wrong-date auction identity')
        if final and item.get('ticker', ts_code[:6]) != ts_code[:6]:
            raise ValueError('native final ticker and exchange identity disagree')
        seen.add(ts_code)
        reason = 'final_snapshot_not_tick_or_order_book_or_preopen_capture' if final else 'historical_bar_not_tick_or_order_book_or_preopen_capture'
        price = _number(item.get('auction_price' if final else 'close'))
        volume = _number(item.get('auction_volume' if final else 'vol'))
        amount = _number(item.get('auction_amount' if final else 'amount'))
        if any(v is None or v <= 0 for v in (price,volume,amount)):
            reason += ';no_positive_price_volume_amount'
        if not final:
            opening, high, low = map(_number, (item.get('open'),item.get('high'),item.get('low')))
            if any(v is None or v<=0 for v in (opening,high,low,price)) or not low<=min(opening,price)<=max(opening,price)<=high:
                reason += ';invalid_native_ohlc'
        output.append(dict(trade_date=trade_date,stock_code=ts_code[:6],
            source_table='hithink_auction_final' if final else 'multi_source_observation',
            confirmation='final_snapshot_observed' if final else 'historical_opening_bar_observed',
            auction_strength=None,auction_amount=amount if amount is not None and amount>=0 else None,
            tick_rows=0,is_fallback=True,missing_reason=reason,
            evidence_json=json.dumps(dict(native=item,provider=provider,
                api='/api/a-share/auction/snapshot' if final else 'stk_auction_o',
                received_at=received_at.isoformat(),raw_payload_hash=payload_hash,
                qualified_tick=False,qualified_order_book=False,predeclared_observation=False,
                volume_unit='native_undocumented'),ensure_ascii=False,sort_keys=True)))
    return output


def resolve_auction_trade_date(db_path: str | Path, requested: str | None = None) -> str:
    if requested:
        return str(requested)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        dates = []
        for relation in (
            "auction_tick", "auction_quote_snapshot",
            "auction_bidding_anomaly", "advanced_morning_bidding_summary",
        ):
            if not table_exists(con, relation):
                continue
            row = con.execute(f'SELECT max(CAST(date AS VARCHAR)) FROM "{relation}"').fetchone()
            if row and row[0]:
                dates.append(str(row[0]))
        return max(dates) if dates else ""
    finally:
        con.close()


def _relation_count(con: duckdb.DuckDBPyConnection, relation: str, trade_date: str | None = None) -> int:
    if not table_exists(con, relation):
        return 0
    if trade_date:
        return int(con.execute(f'SELECT count(*) FROM "{relation}" WHERE CAST(date AS VARCHAR)=?', [trade_date]).fetchone()[0])
    return int(con.execute(f'SELECT count(*) FROM "{relation}"').fetchone()[0])


def ensure_auction_evidence_tables(db_path: str | Path, *, connection=None) -> None:
    from trade_system.db_utils import legacy_connect
    con = connection if connection is not None else legacy_connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS auction_evidence_snapshot (
                trade_date VARCHAR,
                stock_code VARCHAR,
                source_table VARCHAR,
                confirmation VARCHAR,
                auction_strength DOUBLE,
                auction_amount DOUBLE,
                tick_rows INTEGER,
                is_fallback BOOLEAN,
                missing_reason VARCHAR,
                evidence_json VARCHAR,
                generated_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE OR REPLACE VIEW v_auction_evidence AS
            SELECT
                trade_date,
                stock_code,
                source_table,
                confirmation,
                auction_strength,
                auction_amount,
                tick_rows,
                is_fallback,
                missing_reason,
                evidence_json,
                generated_at
            FROM auction_evidence_snapshot
            """
        )
    finally:
        if connection is None:
            con.close()



def _tick_rows(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict]:
    if _relation_count(con, "auction_tick", trade_date) == 0:
        return []
    columns = set(table_columns(con, "auction_tick"))
    unit_expr = "volume_unit" if "volume_unit" in columns else "'unknown'"
    normalized_volume = (
        f"CASE lower(coalesce(nullif({unit_expr}, ''), 'unknown')) "
        "WHEN 'hands' THEN volume * 100 WHEN 'shares' THEN volume ELSE NULL END"
    )
    amount_expr = (
        f"CASE lower(coalesce(nullif({unit_expr}, ''), 'unknown')) "
        "WHEN 'hands' THEN price * volume * 100 "
        "WHEN 'shares' THEN price * volume ELSE NULL END"
    )
    return _fetch_dicts(
        con,
        f"""
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            stock_code,
            count(*) AS tick_rows,
            count(*) FILTER (WHERE {normalized_volume} IS NULL) AS unknown_unit_rows,
            sum({amount_expr}) AS auction_amount,
            sum({normalized_volume}) AS tick_volume,
            max({unit_expr}) AS tick_volume_unit,
            max(time) AS last_tick_time
        FROM auction_tick
        WHERE CAST(date AS VARCHAR)=?
        GROUP BY date, stock_code
        ORDER BY auction_amount DESC NULLS LAST, stock_code
        """,
        [trade_date],
    )


def _anomaly_rows(con: duckdb.DuckDBPyConnection, trade_date: str, tick_codes: set[str]) -> list[dict]:
    if _relation_count(con, "auction_bidding_anomaly", trade_date) == 0:
        return []
    rows = _fetch_dicts(
        con,
        """
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            stock_code,
            anomaly_type,
            max(anomaly_value) AS anomaly_value
        FROM auction_bidding_anomaly
        WHERE CAST(date AS VARCHAR)=?
        GROUP BY date, stock_code, anomaly_type
        ORDER BY anomaly_value DESC NULLS LAST, stock_code
        """,
        [trade_date],
    )
    return [row for row in rows if str(row.get("stock_code") or "") not in tick_codes]


def _quote_rows(
    con: duckdb.DuckDBPyConnection, trade_date: str, stronger_codes: set[str]
) -> list[dict]:
    if _relation_count(con, "auction_quote_snapshot", trade_date) == 0:
        return []
    columns = set(table_columns(con, "auction_quote_snapshot"))
    unit_expr = "volume_unit" if "volume_unit" in columns else "'unknown'"
    rows = _fetch_dicts(
        con,
        f"""
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            stock_code,
            count(*) AS quote_rows,
            max(indicative_price) AS indicative_price,
            max(cumulative_volume) AS cumulative_volume,
            max({unit_expr}) AS volume_unit,
            arg_max(order_imbalance, fetched_at) AS order_imbalance,
            max(quote_time) AS last_quote_time,
            string_agg(DISTINCT provider, ',') AS providers
        FROM auction_quote_snapshot
        WHERE CAST(date AS VARCHAR)=?
        GROUP BY date, stock_code
        ORDER BY abs(arg_max(order_imbalance, fetched_at)) DESC NULLS LAST,
                 max(cumulative_volume) DESC NULLS LAST
        """,
        [trade_date],
    )
    return [
        row for row in rows
        if str(row.get("stock_code") or "") not in stronger_codes
    ]


def _summary_rows(con: duckdb.DuckDBPyConnection, trade_date: str, has_stock_evidence: bool) -> list[dict]:
    if has_stock_evidence or _relation_count(con, "advanced_morning_bidding_summary", trade_date) == 0:
        return []
    return _fetch_dicts(
        con,
        """
        SELECT
            CAST(date AS VARCHAR) AS trade_date,
            total_amount,
            limit_up_count,
            limit_down_count
        FROM advanced_morning_bidding_summary
        WHERE CAST(date AS VARCHAR)=?
        ORDER BY date DESC
        LIMIT 1
        """,
        [trade_date],
    )


def build_auction_evidence_snapshot(db_path: str | Path, trade_date: str) -> list[dict[str, Any]]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {
            "auction_tick": _relation_count(con, "auction_tick", trade_date),
            "auction_quote_snapshot": _relation_count(
                con, "auction_quote_snapshot", trade_date
            ),
            "auction_bidding_anomaly": _relation_count(con, "auction_bidding_anomaly", trade_date),
            "advanced_morning_bidding_summary": _relation_count(con, "advanced_morning_bidding_summary", trade_date),
        }
        output: list[dict[str, Any]] = []
        tick_codes: set[str] = set()
        for row in _tick_rows(con, trade_date):
            tick_codes.add(str(row["stock_code"]))
            qualified = not row.get('unknown_unit_rows')
            strength = float(row.get("tick_volume") or 0) / 1000000.0 if qualified else None
            evidence = {**row, "source_priority": 1, "counts": counts}
            output.append(
                {
                    "trade_date": row["trade_date"],
                    "stock_code": row["stock_code"],
                    "source_table": "auction_tick",
                    "confirmation": "tick_confirmed" if qualified else "tick_observed_unit_unknown",
                    "auction_strength": round(strength, 4) if qualified else None,
                    "auction_amount": row.get("auction_amount") if qualified else None,
                    "tick_rows": int(row.get("tick_rows") or 0),
                    "is_fallback": False,
                    "missing_reason": "" if qualified else "auction_tick_volume_unit_unknown",
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                }
            )

        quote_codes: set[str] = set()
        for row in _quote_rows(con, trade_date, tick_codes):
            quote_codes.add(str(row["stock_code"]))
            volume = float(row.get("cumulative_volume") or 0)
            volume_unit = str(row.get("volume_unit") or "unknown").lower()
            imbalance = float(row.get("order_imbalance") or 0)
            price = float(row.get("indicative_price") or 0)
            # Tencent volume is reported in board lots.  Keep the amount
            # estimate explicit in evidence_json rather than presenting it as
            # exchange transaction turnover.
            if price and volume and volume_unit in {"hands", "shares"}:
                amount_estimate = price * volume * (100 if volume_unit == "hands" else 1)
            else:
                amount_estimate = None
            normalized_volume = volume * (100 if volume_unit == "hands" else 1) if volume_unit in {"hands", "shares"} else 0
            strength = round(imbalance * 100 + min(normalized_volume / 100000.0, 20), 4)
            evidence = {
                **row, "source_priority": 2, "counts": counts,
                "semantic": "auction_order_book_snapshot_not_trade_tick",
                "estimated_amount": amount_estimate,
            }
            output.append(
                {
                    "trade_date": row["trade_date"],
                    "stock_code": row["stock_code"],
                    "source_table": "auction_quote_snapshot",
                    "confirmation": "quote_confirmed",
                    "auction_strength": strength,
                    "auction_amount": amount_estimate,
                    "tick_rows": 0,
                    "is_fallback": False,
                    "missing_reason": "auction_tick_missing;using_verified_order_book_snapshot",
                    "evidence_json": json.dumps(
                        evidence, ensure_ascii=False, sort_keys=True
                    ),
                }
            )

        for row in _anomaly_rows(con, trade_date, tick_codes | quote_codes):
            value = float(row.get("anomaly_value") or 0)
            evidence = {**row, "source_priority": 3, "counts": counts}
            output.append(
                {
                    "trade_date": row["trade_date"],
                    "stock_code": row["stock_code"],
                    "source_table": "auction_bidding_anomaly",
                    "confirmation": "anomaly_confirmed" if value > 0 else "anomaly_watch",
                    "auction_strength": value,
                    "auction_amount": None,
                    "tick_rows": 0,
                    "is_fallback": True,
                    "missing_reason": "auction_tick_missing",
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                }
            )

        for row in _summary_rows(con, trade_date, bool(output)):
            limit_up = int(row.get("limit_up_count") or 0)
            limit_down = int(row.get("limit_down_count") or 0)
            amount = float(row.get("total_amount") or 0)
            strength = round(amount / 100000000.0 + limit_up * 1.5 - limit_down, 4)
            evidence = {**row, "source_priority": 4, "counts": counts}
            output.append(
                {
                    "trade_date": row["trade_date"],
                    "stock_code": None,
                    "source_table": "advanced_morning_bidding_summary",
                    "confirmation": "market_confirmed" if limit_up > limit_down else "market_watch",
                    "auction_strength": strength,
                    "auction_amount": amount,
                    "tick_rows": 0,
                    "is_fallback": True,
                    "missing_reason": "auction_tick_missing;stock_level_evidence_missing",
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                }
            )
        return output
    finally:
        con.close()


def persist_auction_evidence_snapshot(db_path: str | Path, rows: list[dict[str, Any]]) -> int:
    ensure_auction_evidence_tables(db_path)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        for row in rows:
            con.execute(
                """
                DELETE FROM auction_evidence_snapshot
                WHERE trade_date = ? AND coalesce(stock_code, '') = coalesce(?, '') AND source_table = ?
                """,
                [row["trade_date"], row.get("stock_code"), row["source_table"]],
            )
            values = [row.get(column) for column in EVIDENCE_COLUMNS]
            placeholders = ", ".join(["?"] * len(EVIDENCE_COLUMNS))
            column_sql = ", ".join(f'"{column}"' for column in EVIDENCE_COLUMNS)
            con.execute(f"INSERT INTO auction_evidence_snapshot ({column_sql}) VALUES ({placeholders})", values)
        con.commit()
        return len(rows)
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()


def build_auction_evidence_status(db_path: str | Path, trade_date: str) -> dict[str, Any]:
    rows = build_auction_evidence_snapshot(db_path, trade_date)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {
            "auction_tick": _relation_count(con, "auction_tick", trade_date),
            "auction_quote_snapshot": _relation_count(
                con, "auction_quote_snapshot", trade_date
            ),
            "auction_bidding_anomaly": _relation_count(con, "auction_bidding_anomaly", trade_date),
            "advanced_morning_bidding_summary": _relation_count(con, "advanced_morning_bidding_summary", trade_date),
        }
    finally:
        con.close()
    gaps = []
    if counts["auction_tick"] == 0:
        gaps.append(
            "auction_tick_missing"
            if counts["auction_quote_snapshot"] == 0
            else "auction_tick_missing_order_book_snapshot_available"
        )
    if not rows:
        gaps.append("auction_evidence_missing")
    return {"trade_date": trade_date, "counts": counts, "rows": rows, "gaps": gaps}


def render_auction_evidence_report(status: dict[str, Any]) -> str:
    lines = [
        "# 竞价证据链报告",
        "",
        f"- Trade date: `{status.get('trade_date', '')}`",
        "",
        "## Source Counts",
        "",
        "| Source | Rows |",
        "|---|---:|",
    ]
    for name, count in sorted(status.get("counts", {}).items()):
        lines.append(f"| `{name}` | {count} |")
    lines.extend(
        [
            "",
            "## Evidence Rows",
            "",
            "| Stock | Source | Confirmation | Strength | Fallback | Missing reason |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for row in status.get("rows", [])[:100]:
        lines.append(
            f"| {row.get('stock_code') or 'MARKET'} | `{row.get('source_table')}` | "
            f"{row.get('confirmation')} | {row.get('auction_strength')} | "
            f"`{row.get('is_fallback')}` | {row.get('missing_reason') or ''} |"
        )
    lines.extend(["", "## Gaps", ""])
    if status.get("gaps"):
        for gap in status["gaps"]:
            lines.append(f"- {gap}")
    else:
        lines.append("- No auction evidence gaps detected.")
    lines.extend(
        [
            "",
            "## Operator Note",
            "",
            "- `auction_tick` is the only full stock-level竞价 tick evidence.",
            "- Fallback rows can support watch/confirm discussion, but must not be treated as tick-confirmed evidence.",
            "",
        ]
    )
    return "\n".join(lines)
