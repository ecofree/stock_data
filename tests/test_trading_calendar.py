from __future__ import annotations

import duckdb

from trade_system.trading_calendar import trading_session_status


def _calendar_db(path, rows):
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE tushare_trade_cal("
        "exchange VARCHAR, cal_date DATE, is_open BOOLEAN, pretrade_date DATE)"
    )
    if rows:
        con.executemany(
            "INSERT INTO tushare_trade_cal(exchange,cal_date,is_open) VALUES (?,?,?)",
            rows,
        )
    con.close()


def test_trading_session_status_is_open_only_from_verified_row(tmp_path):
    db = tmp_path / "open.duckdb"
    _calendar_db(db, [("SSE", "2026-07-27", True)])
    status = trading_session_status(db, "2026-07-27")
    assert status.state == "open"
    assert status.is_open is True


def test_trading_session_status_distinguishes_closed_and_missing(tmp_path):
    db = tmp_path / "closed.duckdb"
    _calendar_db(db, [("SSE", "2026-07-27", False)])
    assert trading_session_status(db, "2026-07-27").state == "closed"
    assert trading_session_status(db, "2026-07-28").state == "unverified"


def test_trading_session_status_rejects_contradictory_rows(tmp_path):
    db = tmp_path / "contradictory.duckdb"
    _calendar_db(
        db,
        [("SSE", "2026-07-27", True), ("SSE", "2026-07-27", False)],
    )
    status = trading_session_status(db, "2026-07-27")
    assert status.state == "unverified"
    assert "contradictory" in status.reason
