"""Import and normalize manual operator outcomes.

These records are human/operator review data. They never create orders.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


from trade_system.quality import table_exists
from trade_system.risk import OPERATOR_PLAN_OUTCOME_VIEW_SQL, init_trading_tables


OUTCOME_COLUMNS = [
    "trade_date",
    "stock_code",
    "stock_name",
    "execution_status",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "position_pct",
    "gross_return_pct",
    "net_return_pct",
    "outcome_tag",
    "mistake_tag",
    "review_note",
    "imported_from",
]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalized_status(value: Any) -> str:
    status = _text(value).lower()
    if status in {"executed", "skipped", "cancelled", "canceled", "not_executed"}:
        return "cancelled" if status == "canceled" else status
    return "review_required"


def ensure_operator_outcome_tables(db_path: str | Path) -> list[str]:
    init_trading_tables(db_path)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_trade_outcome (
                trade_date VARCHAR,
                stock_code VARCHAR,
                stock_name VARCHAR,
                execution_status VARCHAR,
                entry_time VARCHAR,
                exit_time VARCHAR,
                entry_price DOUBLE,
                exit_price DOUBLE,
                position_pct DOUBLE,
                gross_return_pct DOUBLE,
                net_return_pct DOUBLE,
                outcome_tag VARCHAR,
                mistake_tag VARCHAR,
                review_note VARCHAR,
                imported_from VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(OPERATOR_PLAN_OUTCOME_VIEW_SQL)
        return ["operator_trade_outcome", "v_operator_plan_outcome"]
    finally:
        con.close()


def _normalize_row(
    row: dict[str, Any],
    imported_from: str,
    fee_rate: float,
    slippage_bps: float,
) -> dict[str, Any]:
    execution_status = _normalized_status(row.get("execution_status"))
    entry_price = _float(row.get("entry_price"))
    exit_price = _float(row.get("exit_price"))
    gross = _float(row.get("gross_return_pct"))
    if gross is None and execution_status == "executed":
        if entry_price is not None and exit_price is not None and entry_price > 0 and exit_price > 0:
            gross = (exit_price - entry_price) * 100.0 / entry_price
    net = _float(row.get("net_return_pct"))
    if net is None and gross is not None and execution_status == "executed":
        net = gross - fee_rate * 100.0 - (slippage_bps / 10000.0) * 100.0
    return {
        "trade_date": _text(row.get("trade_date")),
        "stock_code": _text(row.get("stock_code")),
        "stock_name": _text(row.get("stock_name")),
        "execution_status": execution_status,
        "entry_time": _text(row.get("entry_time")),
        "exit_time": _text(row.get("exit_time")),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "position_pct": _float(row.get("position_pct")) or 0.0,
        "gross_return_pct": round(float(gross), 4) if gross is not None else None,
        "net_return_pct": round(float(net), 4) if net is not None else None,
        "outcome_tag": _text(row.get("outcome_tag")) or "unclassified",
        "mistake_tag": _text(row.get("mistake_tag")) or "unreviewed",
        "review_note": _text(row.get("review_note")),
        "imported_from": imported_from,
    }


def import_operator_trade_outcomes(
    db_path: str | Path,
    csv_path: str | Path,
    *,
    fee_rate: float = 0.001,
    slippage_bps: float = 10.0,
) -> dict[str, int]:
    """Import manually reviewed outcomes from CSV.

    Required CSV columns: trade_date, stock_code, execution_status.
    Optional columns are listed in OUTCOME_COLUMNS. Existing rows for the same
    trade_date/stock_code are replaced so reruns are idempotent.
    """

    ensure_operator_outcome_tables(db_path)
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [
            _normalize_row(row, path.name, fee_rate, slippage_bps)
            for row in reader
            if _text(row.get("trade_date")) and _text(row.get("stock_code"))
        ]

    # The exported template deliberately contains review placeholders.  Do
    # not let an untouched template create fake performance rows or move a
    # plan to ``review_required`` while the operator has supplied no result.
    # Validation happens before opening the write connection, so a mixed CSV
    # cannot partially import before the first unreviewed row is discovered.
    unreviewed = [
        f"{row['trade_date']}:{row['stock_code']}"
        for row in rows
        if row["execution_status"] == "review_required"
        or (
            row["execution_status"] == "executed"
            and (
                row["outcome_tag"].lower() in {"", "unclassified", "review_required"}
                or row["mistake_tag"].lower() in {"", "unreviewed", "review_required"}
            )
        )
    ]
    if unreviewed:
        sample = ", ".join(unreviewed[:5])
        suffix = "..." if len(unreviewed) > 5 else ""
        raise ValueError(
            "operator outcome CSV still contains unreviewed rows; "
            f"fill execution_status/outcome_tag/mistake_tag before import: {sample}{suffix}"
        )

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        rows_imported = 0
        journal_rows = 0
        plans_updated = 0
        for row in rows:
            con.execute(
                "DELETE FROM operator_trade_outcome WHERE trade_date = ? AND stock_code = ?",
                [row["trade_date"], row["stock_code"]],
            )
            con.execute(
                "DELETE FROM trade_journal WHERE trade_date = ? AND stock_code = ? AND action = 'operator_outcome'",
                [row["trade_date"], row["stock_code"]],
            )
            con.execute(
                """
                INSERT INTO operator_trade_outcome (
                    trade_date, stock_code, stock_name, execution_status, entry_time, exit_time,
                    entry_price, exit_price, position_pct, gross_return_pct, net_return_pct,
                    outcome_tag, mistake_tag, review_note, imported_from
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [row[column] for column in OUTCOME_COLUMNS],
            )
            rows_imported += 1

            if table_exists(con, "trade_plan"):
                matched_plans = int(
                    con.execute(
                        "SELECT count(*) FROM trade_plan WHERE trade_date = ? AND stock_code = ?",
                        [row["trade_date"], row["stock_code"]],
                    ).fetchone()[0]
                )
                con.execute(
                    """
                    UPDATE trade_plan
                    SET status = ?
                    WHERE trade_date = ? AND stock_code = ?
                    """,
                    [row["execution_status"], row["trade_date"], row["stock_code"]],
                )
                plans_updated += matched_plans

            con.execute(
                """
                INSERT INTO trade_journal (
                    trade_date, stock_code, stock_name, action, action_time, price,
                    position_pct, reason, mistake_tag
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["trade_date"],
                    row["stock_code"],
                    row["stock_name"],
                    "operator_outcome",
                    row["exit_time"] or row["entry_time"] or "after_close",
                    row["exit_price"] if row["exit_price"] is not None else row["entry_price"],
                    row["position_pct"],
                    row["review_note"] or row["outcome_tag"],
                    row["mistake_tag"],
                ],
            )
            journal_rows += 1
        return {"rows_imported": rows_imported, "journal_rows": journal_rows, "plans_updated": plans_updated}
    finally:
        con.close()
