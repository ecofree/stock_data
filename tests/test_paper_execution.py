import duckdb
import pytest

from trade_system.paper_execution import (
    release_t1_sellable,
    simulate_fill,
    submit_paper_order,
)


def test_paper_order_is_fail_closed_and_idempotent(tmp_path):
    db = tmp_path / "paper.duckdb"
    blocked = submit_paper_order(
        db,
        order_id="o-blocked",
        idempotency_key="same-blocked",
        trade_date="2026-08-10",
        stock_code="000001",
        side="buy",
        quantity=100,
        limit_price=10,
        candidate_model_id="shadow",
    )
    assert blocked["status"] == "rejected"
    duplicate = submit_paper_order(
        db,
        order_id="o-other",
        idempotency_key="same-blocked",
        trade_date="2026-08-10",
        stock_code="000001",
        side="buy",
        quantity=100,
        limit_price=10,
        candidate_model_id="shadow",
    )
    assert duplicate["order_id"] == "o-blocked"


def test_paper_buy_obeys_t1_before_sell(tmp_path):
    db = tmp_path / "paper_t1.duckdb"
    submitted = submit_paper_order(
        db,
        order_id="o-buy",
        idempotency_key="buy-1",
        trade_date="2026-08-10",
        stock_code="000001",
        side="buy",
        quantity=100,
        limit_price=10,
        candidate_model_id="champion",
        risk_approved=True,
        tradable=True,
    )
    assert submitted["status"] == "pending"
    assert simulate_fill(db, order_id="o-buy", fill_price=10)["status"] == "filled"
# (removed dead assignment flagged by F841)
    _unused_sell = submit_paper_order(
        db,
        order_id="o-sell",
        idempotency_key="sell-1",
        trade_date="2026-08-10",
        stock_code="000001",
        side="sell",
        quantity=100,
        limit_price=10.2,
        candidate_model_id="champion",
        risk_approved=True,
        tradable=True,
    )
    with pytest.raises(ValueError, match=r"T\+1"):
        simulate_fill(db, order_id="o-sell", fill_price=10.2)
    assert release_t1_sellable(db, "2026-08-11") == 1
    assert simulate_fill(db, order_id="o-sell", fill_price=10.2)["status"] == "filled"
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT quantity FROM paper_position WHERE stock_code='000001'").fetchone()[0] == 0
    finally:
        con.close()


def test_paper_partial_fill_can_be_completed(tmp_path):
    db = tmp_path / "paper_partial.duckdb"
    submit_paper_order(
        db,
        order_id="o-partial",
        idempotency_key="partial-1",
        trade_date="2026-08-10",
        stock_code="000002",
        side="buy",
        quantity=200,
        limit_price=5,
        candidate_model_id="champion",
        risk_approved=True,
        tradable=True,
    )
    first = simulate_fill(db, order_id="o-partial", fill_price=5, fill_quantity=100)
    second = simulate_fill(db, order_id="o-partial", fill_price=5.1, fill_quantity=100)
    assert first["status"] == "partially_filled"
    assert second["status"] == "filled"
    assert second["filled_quantity"] == 200
