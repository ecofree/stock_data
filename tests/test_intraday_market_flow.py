from __future__ import annotations

from datetime import date as system_date, datetime as system_datetime

import duckdb
import requests

import trade_system.eastmoney_finance as eastmoney
from trade_system.eastmoney_clist_guard import EastmoneyClistGuard
from scripts.collect_intraday_stock_flow_market import (
    _a_share_universe_by_exchange,
    collect_market_stock_flow,
)


def _market_row(code: str, trade_date: str = "2026-07-14 00:00:00"):
    return {
        "SECURITY_CODE": code,
        "TRADE_DATE": trade_date,
        "SECURITY_NAME_ABBR": "Test",
        "PRIME_INFLOW": 100.0,
        "SUPERDEAL_INFLOW": 70.0,
        "SUPERDEAL_OUTFLOW": 20.0,
        "BIGDEAL_INFLOW": 50.0,
        "BIGDEAL_OUTFLOW": 10.0,
        "CLOSE_PRICE": 10.0,
        "CHANGE_RATE": 2.0,
        "TURNOVERRATE": 3.0,
    }


def test_market_flow_fetcher_paginates_and_normalizes(monkeypatch):
    def fake_curl(url):
        page = int(url.split("pageNumber=")[1].split("&")[0])
        if page == 1:
            return {"result": {"pages": 2, "count": 2, "data": [_market_row("000001")]}}
        return {"result": {"pages": 2, "count": 2, "data": [_market_row("600000")]}}

    monkeypatch.setattr(eastmoney, "_curl_json", fake_curl)
    rows, meta = eastmoney.get_fund_flow_market("2026-07-14", pause_seconds=0)
    assert {row["code"] for row in rows} == {"000001", "600000"}
    assert rows[0]["main_net"] == 100.0
    assert rows[0]["super_net"] == 50.0
    assert meta["pages"] == 2
    assert meta["expected_rows"] == 2


def test_market_flow_batch_persists_each_page_and_coverage(tmp_path, monkeypatch):
    def fake_fetch(trade_date, *, page_size, max_pages, pause_seconds, on_page):
        page1 = [_market_row("000001")]
        page2 = [_market_row("600000")]
        on_page(1, page1, 2, {"count": 2})
        on_page(2, page2, 2, {"count": 2})
        return [], {"pages": 2, "expected_rows": 2, "rows": 2}

    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.get_fund_flow_market",
        fake_fetch,
    )
    db = tmp_path / "market.duckdb"
    result = collect_market_stock_flow(db, "2026-07-14", pause_seconds=0)
    assert result["status"] == "success"
    assert result["fetched_rows"] == 2
    assert result["coverage_pct"] == 100.0
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select count(*) from multi_source_stock_flow").fetchone()[0] == 2
        assert con.execute("select count(*) from intraday_stock_flow_page_checkpoint where status='success'").fetchone()[0] == 2
    finally:
        con.close()


def test_fresh_market_flow_refresh_rolls_back_partial_publish(tmp_path, monkeypatch):
    calls = {"count": 0}

    def fake_fetch(trade_date, *, page_size, max_pages, pause_seconds, on_page):
        calls["count"] += 1
        on_page(1, [_market_row("000001", "2026-07-14 00:00:00")], 2, {"count": 2})
        if calls["count"] > 1:
            raise RuntimeError("provider reset after page one")
        on_page(2, [_market_row("600000", "2026-07-14 00:00:00")], 2, {"count": 2})
        return [], {"pages": 2, "expected_rows": 2, "rows": 2}

    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.get_fund_flow_market",
        fake_fetch,
    )
    db = tmp_path / "atomic_market.duckdb"
    first = collect_market_stock_flow(db, "2026-07-14", pause_seconds=0)
    second = collect_market_stock_flow(db, "2026-07-14", pause_seconds=0)
    assert first["status"] == "success"
    assert second["status"] == "partial"
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "select count(*) from multi_source_stock_flow where source_date='2026-07-14' and provider='eastmoney_market'"
        ).fetchone()[0] == 2
    finally:
        con.close()


def test_second_atomic_refresh_updates_same_unique_keys_without_conflict(tmp_path, monkeypatch):
    calls = {"count": 0}

    def fake_fetch(trade_date, *, page_size, max_pages, pause_seconds, on_page):
        calls["count"] += 1
        amount = 100.0 + calls["count"]
        row1 = _market_row("000001", "2026-07-14 00:00:00")
        row2 = _market_row("920992", "2026-07-14 00:00:00")
        row1["PRIME_INFLOW"] = amount
        row2["PRIME_INFLOW"] = amount
        on_page(1, [row1, row2], 1, {
            "count": 2, "source": "eastmoney_market", "status": "live",
        })
        return [], {
            "pages": 1, "expected_rows": 2, "rows": 2,
            "source": "eastmoney_market",
        }

    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.get_fund_flow_market",
        fake_fetch,
    )
    db = tmp_path / "second-atomic-refresh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE tushare_stock_basic(stock_code VARCHAR, ts_code VARCHAR)")
    con.execute(
        "INSERT INTO tushare_stock_basic VALUES "
        "('000001','000001.SZ'),('920992','920992.BJ')"
    )
    con.close()

    first = collect_market_stock_flow(db, "2026-07-14", pause_seconds=0)
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE UNIQUE INDEX uq_test_stock_flow ON multi_source_stock_flow"
        "(source_date,stock_code,provider)"
    )
    con.close()
    second = collect_market_stock_flow(db, "2026-07-14", pause_seconds=0)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert second["error"] == ""
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT count(*) FROM multi_source_stock_flow"
        ).fetchone()[0] == 2
        assert con.execute(
            "SELECT min(main_net),max(main_net) FROM multi_source_stock_flow"
        ).fetchone() == (102.0, 102.0)
    finally:
        con.close()


def test_market_flow_filters_b_shares_and_cross_page_duplicates(tmp_path, monkeypatch):
    def fake_fetch(trade_date, *, page_size, max_pages, pause_seconds, on_page):
        on_page(1, [_market_row("000001"), _market_row("900948")], 2, {"count": 4})
        on_page(2, [_market_row("000001"), _market_row("600000")], 2, {"count": 4})
        return [], {"pages": 2, "expected_rows": 4, "rows": 4}

    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.get_fund_flow_market",
        fake_fetch,
    )
    db = tmp_path / "canonical_market.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE tushare_stock_basic(stock_code VARCHAR, ts_code VARCHAR)"
    )
    con.execute(
        "INSERT INTO tushare_stock_basic VALUES ('000001','000001.SZ'),('600000','600000.SH'),('900948','900948.SH')"
    )
    con.close()

    result = collect_market_stock_flow(db, "2026-07-14", pause_seconds=0)
    assert result["status"] == "success"
    assert result["expected_rows"] == 2
    assert result["fetched_rows"] == 2
    assert result["coverage_pct"] == 100.0
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT array_agg(stock_code ORDER BY stock_code) FROM multi_source_stock_flow"
        ).fetchone()[0] == ["000001", "600000"]
    finally:
        con.close()


def test_a_share_universe_keeps_beijing_but_excludes_exchange_specific_b_shares(tmp_path):
    db = tmp_path / "universe.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE tushare_stock_basic(stock_code VARCHAR, ts_code VARCHAR)")
    con.execute(
        "INSERT INTO tushare_stock_basic VALUES "
        "('000001','000001.SZ'),('600000','600000.SH'),"
        "('920001','920001.BJ'),('900948','900948.SH'),('200012','200012.SZ')"
    )
    try:
        assert _a_share_universe_by_exchange(con) == {
            "000001": "SZ",
            "600000": "SH",
            "920001": "BJ",
        }
    finally:
        con.close()


def test_market_flow_persists_beijing_exchange_coverage(tmp_path, monkeypatch):
    def fake_fetch(trade_date, *, page_size, max_pages, pause_seconds, on_page):
        rows = [_market_row("000001"), _market_row("600000"), _market_row("920001")]
        on_page(1, rows, 1, {"count": 3})
        return [], {"pages": 1, "expected_rows": 3, "rows": 3}

    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.get_fund_flow_market",
        fake_fetch,
    )
    db = tmp_path / "bj-coverage.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE tushare_stock_basic(stock_code VARCHAR, ts_code VARCHAR)")
    con.execute(
        "INSERT INTO tushare_stock_basic VALUES "
        "('000001','000001.SZ'),('600000','600000.SH'),('920001','920001.BJ')"
    )
    con.close()

    result = collect_market_stock_flow(db, "2026-07-14", pause_seconds=0)
    assert result["status"] == "success"
    assert result["exchange_coverage"]["BJ"]["coverage_pct"] == 100.0
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT expected_rows,fetched_rows,status "
            "FROM intraday_stock_flow_exchange_coverage WHERE exchange='BJ'"
        ).fetchone() == (1, 1, "success")
    finally:
        con.close()


def test_after_close_reconciliation_does_not_duplicate_reference_snapshot(
    tmp_path, monkeypatch
):
    trade_date = system_date.today().isoformat()

    class _AfterCloseDateTime:
        @classmethod
        def now(cls):
            return system_datetime.combine(
                system_date.today(), system_datetime.strptime("17:45", "%H:%M").time()
            )

    def fake_live(trade_date, *, page_size, max_pages, pause_seconds, on_page, start_page):
        rows = [
            {
                "f12": code, "f14": "Test", "f2": 10.0, "f3": 2.0,
                "f62": 100.0, "f66": 50.0, "f72": 40.0,
                "f78": 10.0, "f84": 0.0,
            }
            for code in ("000001", "600000")
        ]
        on_page(
            1, rows, 1,
            {"count": 2, "source": "eastmoney_intraday_clist", "status": "live"},
        )
        return [], {
            "pages": 1, "expected_rows": 2, "rows": 2,
            "source": "eastmoney_intraday_clist",
        }

    def fake_reference(trade_date, *, page_size, pause_seconds):
        return [
            {
                "code": code,
                "date": trade_date,
                "main_net": 100.0,
                "super_net": 50.0,
                "large_net": 40.0,
                "mid_net": 10.0,
                "small_net": 0.0,
            }
            for code in ("000001", "600000")
        ], {"source": "eastmoney_market"}

    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.datetime",
        _AfterCloseDateTime,
    )
    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.get_fund_flow_market_realtime",
        fake_live,
    )
    monkeypatch.setattr(
        "scripts.collect_intraday_stock_flow_market.get_fund_flow_market",
        fake_reference,
    )
    db = tmp_path / "after-close-reconciliation.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE tushare_stock_basic(stock_code VARCHAR, ts_code VARCHAR)")
    con.execute(
        "INSERT INTO tushare_stock_basic VALUES "
        "('000001','000001.SZ'),('600000','600000.SH')"
    )
    con.close()

    result = collect_market_stock_flow(db, trade_date, pause_seconds=0)

    assert result["status"] == "success", result.get("error")
    assert result["reconciliation"]["status"] == "pass"
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT status FROM intraday_stock_flow_batch WHERE trade_date=?",
            [trade_date],
        ).fetchone()[0] == "success"
        assert con.execute(
            "SELECT count(*) FROM multi_source_stock_flow "
            "WHERE source_date=? AND provider='eastmoney_market'",
            [trade_date],
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_realtime_market_flow_normalizes_push2_rows(monkeypatch, tmp_path):
    # Keep the persistent production circuit-breaker out of the test run.
    monkeypatch.setattr(eastmoney, "DEFAULT_CLIST_GUARD", EastmoneyClistGuard(tmp_path / "guard.json"))
    payloads = [
        {"data": {"total": 1, "diff": [{
            "f12": "000001", "f14": "平安银行", "f2": 12.3, "f3": 1.2,
            "f62": 1000, "f66": 500, "f72": 300, "f78": 200, "f84": 0,
            "f184": 2.5,
        }]}}
    ]

    class _Response:
        content = (b'{"data":{"total":1,"diff":[{"f12":"000001",'
                   b'"f14":"Test","f2":12.3,"f3":1.2,"f62":1000,'
                   b'"f66":500,"f72":300,"f78":200,"f84":0,"f184":2.5}]}}')

        def raise_for_status(self):
            return None

        def json(self):
            return payloads[0]

    class _Session:
        trust_env = True

        def get(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(requests, "Session", lambda: _Session())
    rows, meta = eastmoney.get_fund_flow_market_realtime(
        "2026-07-15", page_size=100, max_pages=1, pause_seconds=0,
    )
    assert rows[0]["code"] == "000001"
    assert rows[0]["date"] == "2026-07-15"
    assert rows[0]["main_net"] == 1000.0
    assert rows[0]["super_net"] == 500.0
    assert meta["source"] == "eastmoney_intraday_clist"
