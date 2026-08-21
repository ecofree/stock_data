"""Paper-only order state machine for execution-path rehearsal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


PAPER_STATUSES = {"pending", "filled", "partially_filled", "rejected", "canceled"}


def ensure_paper_tables(db_path: str | Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_order (
                order_id VARCHAR PRIMARY KEY,
                idempotency_key VARCHAR UNIQUE,
                trade_date VARCHAR,
                stock_code VARCHAR,
                side VARCHAR,
                quantity INTEGER,
                filled_quantity INTEGER DEFAULT 0,
                limit_price DOUBLE,
                avg_fill_price DOUBLE,
                status VARCHAR,
                reason VARCHAR,
                candidate_model_id VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp,
                updated_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_position (
                stock_code VARCHAR PRIMARY KEY,
                quantity INTEGER,
                sellable_quantity INTEGER,
                avg_cost DOUBLE,
                last_trade_date VARCHAR,
                updated_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
    finally:
        con.close()


def submit_paper_order(
    db_path: str | Path,
    *,
    order_id: str,
    idempotency_key: str,
    trade_date: str,
    stock_code: str,
    side: str,
    quantity: int,
    limit_price: float,
    candidate_model_id: str,
    risk_approved: bool = False,
    tradable: bool = False,
) -> dict[str, Any]:
    """Create an idempotent paper order; never contacts a broker."""
    ensure_paper_tables(db_path)
    side = str(side).lower().strip()
    quantity = int(quantity)
    if side not in {"buy", "sell"}:
        raise ValueError("paper order side must be buy or sell")
    if quantity < 100 or quantity % 100 != 0:
        raise ValueError("A-share paper quantity must be a positive 100-share lot")
    if float(limit_price) <= 0:
        raise ValueError("limit_price must be positive")
    con = duckdb.connect(str(db_path))
    try:
        existing = con.execute(
            "SELECT order_id, status, filled_quantity FROM paper_order WHERE idempotency_key = ?",
            [idempotency_key],
        ).fetchone()
        if existing:
            return {"order_id": existing[0], "status": existing[1], "filled_quantity": existing[2], "idempotent": True}
        approved = bool(risk_approved and tradable)
        status = "pending" if approved else "rejected"
        reason = "paper_ready" if approved else "risk_or_tradability_gate_blocked"
        con.execute(
            """
            INSERT INTO paper_order (
                order_id, idempotency_key, trade_date, stock_code, side, quantity,
                filled_quantity, limit_price, avg_fill_price, status, reason,
                candidate_model_id
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?)
            """,
            [order_id, idempotency_key, trade_date, stock_code, side, quantity, float(limit_price), status, reason, candidate_model_id],
        )
        return {"order_id": order_id, "status": status, "filled_quantity": 0, "idempotent": False, "reason": reason}
    finally:
        con.close()


def simulate_fill(
    db_path: str | Path,
    *,
    order_id: str,
    fill_price: float | None,
    fill_quantity: int | None = None,
    available: bool = True,
    price_limit_blocked: bool = False,
) -> dict[str, Any]:
    ensure_paper_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT trade_date, stock_code, side, quantity, filled_quantity, status FROM paper_order WHERE order_id = ?",
            [order_id],
        ).fetchone()
        if not row:
            raise ValueError(f"paper order not found: {order_id}")
        trade_date, stock_code, side, quantity, existing_filled, status = row
        if status not in {"pending", "partially_filled"}:
            return {"order_id": order_id, "status": status, "filled_quantity": 0, "idempotent": True}
        if not available or price_limit_blocked or fill_price is None or float(fill_price) <= 0:
            reason = "quote_unavailable" if not available else "price_limit_or_invalid_fill"
            con.execute(
                "UPDATE paper_order SET status='rejected', reason=?, updated_at=current_timestamp WHERE order_id=?",
                [reason, order_id],
            )
            return {"order_id": order_id, "status": "rejected", "filled_quantity": 0, "reason": reason}
        remaining = int(quantity) - int(existing_filled or 0)
        fill = min(remaining, max(0, int(fill_quantity if fill_quantity is not None else remaining)))
        if fill <= 0:
            raise ValueError("fill_quantity must be positive")
        position = None
        if side == "sell":
            position = con.execute(
                "SELECT quantity, sellable_quantity FROM paper_position WHERE stock_code=?",
                [stock_code],
            ).fetchone()
            if not position or fill > int(position[1] or 0):
                raise ValueError("T+1 sellable quantity is insufficient")
        cumulative_fill = int(existing_filled or 0) + fill
        final_status = "filled" if cumulative_fill == quantity else "partially_filled"
        con.execute(
            "UPDATE paper_order SET filled_quantity=?, avg_fill_price=?, status=?, reason='simulated_fill', updated_at=current_timestamp WHERE order_id=?",
            [cumulative_fill, float(fill_price), final_status, order_id],
        )
        if side == "buy":
            existing = con.execute(
                "SELECT quantity, sellable_quantity, avg_cost FROM paper_position WHERE stock_code = ?",
                [stock_code],
            ).fetchone()
            old_qty, old_sellable, old_cost = existing if existing else (0, 0, 0.0)
            new_qty = int(old_qty or 0) + fill
            new_cost = ((int(old_qty or 0) * float(old_cost or 0.0)) + fill * float(fill_price)) / new_qty
            con.execute("DELETE FROM paper_position WHERE stock_code = ?", [stock_code])
            con.execute(
                "INSERT INTO paper_position (stock_code, quantity, sellable_quantity, avg_cost, last_trade_date) VALUES (?, ?, ?, ?, ?)",
                [stock_code, new_qty, int(old_sellable or 0), new_cost, str(trade_date)],
            )
        else:
            con.execute(
                "UPDATE paper_position SET quantity=quantity-?, sellable_quantity=sellable_quantity-?, last_trade_date=?, updated_at=current_timestamp WHERE stock_code=?",
                [fill, fill, str(trade_date), stock_code],
            )
        return {"order_id": order_id, "status": final_status, "filled_quantity": cumulative_fill, "fill_quantity_delta": fill, "fill_price": float(fill_price)}
    finally:
        con.close()


def release_t1_sellable(db_path: str | Path, trade_date: str) -> int:
    """Make prior-day paper buys sellable; same-day buys remain blocked."""
    ensure_paper_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "UPDATE paper_position SET sellable_quantity=quantity, updated_at=current_timestamp WHERE last_trade_date < ?",
            [str(trade_date)],
        )
        return int(con.execute("SELECT count(*) FROM paper_position WHERE sellable_quantity > 0").fetchone()[0])
    finally:
        con.close()
