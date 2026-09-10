"""L2 focus collection + KPL-stale boost decisions."""

from __future__ import annotations


import duckdb

from trade_system.kpl_health import (
    boosted_l2_stock_limit,
    boosted_quote_limit,
    kpl_consecutive_stale,
    should_boost_alternative_sources,
)
from trade_system.l2_focus import (
    _new_staging_store,
    _write_eastmoney_trends_fallback,
    collect_l2_focus,
    ensure_l2_focus_checkpoint,
)


class _FakeClient:
    def __init__(self, payload_by_code: dict):
        self.payload_by_code = payload_by_code
        self.stats = {
            "success": 0,
            "error": 0,
            "empty": 0,
            "semantic_error": 0,
            "rate_limited": 0,
            "skipped": 0,
            "circuit_open": 0,
        }
        self._circuit_open_reason = None
        self.calls = []

    def get(self, endpoint, params=None, **kwargs):
        self.calls.append((endpoint, dict(params or {})))
        code = str((params or {}).get("code") or "")
        data = self.payload_by_code.get(code)
        if data is None:
            self.stats["empty"] += 1
            return None
        self.stats["success"] += 1
        return data


def test_eastmoney_l2_fallback_stops_when_shared_budget_is_exhausted(
    monkeypatch,
):
    called = []
    monkeypatch.setattr(
        "trade_system.stock_data_sources._from_em_trends",
        lambda *args, **kwargs: called.append(args),
    )
    store = _new_staging_store()
    try:
        result = _write_eastmoney_trends_fallback(
            store,
            "2026-07-31",
            ["000001", "000002"],
            deadline=0,
        )
    finally:
        store.close()
    assert result == {"intraday_rows": 0, "stock_codes_ok": 0}
    assert called == []


def test_kpl_stale_boost_threshold(tmp_path):
    db = tmp_path / "stale.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE kpl_stale_tracker("
        "date DATE PRIMARY KEY, consecutive_stale INTEGER, updated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO kpl_stale_tracker VALUES ('2026-07-31', 5, current_timestamp)"
    )
    con.close()
    assert kpl_consecutive_stale(str(db), "2026-07-31") == 5
    info = should_boost_alternative_sources(str(db), "2026-07-31", threshold=3)
    assert info["boost"] is True
    assert boosted_quote_limit(200, boost=True) == 400
    assert boosted_l2_stock_limit(40, boost=True) == 80


def test_collect_l2_focus_writes_intraday_for_candidates(tmp_path):
    db = tmp_path / "l2.duckdb"
    trade_date = "2026-07-31"
    con = duckdb.connect(str(db))
    # Minimal schema pieces used by init_schema may be heavy; create only tables
    # the collector needs and bypass full init via direct tables + fake client.
    con.execute(
        """
        CREATE TABLE l2_stock_intraday(
            date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE,
            avg_price DOUBLE, volume BIGINT, turnover BIGINT, main_fund_net BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE l2_stock_bigorder(
            date DATE, stock_code VARCHAR, time VARCHAR, big_net_amount BIGINT,
            big_buy BIGINT, big_sell BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        "CREATE TABLE l2_realtime_all_boards("
        "date DATE, board_level INTEGER, stock_code VARCHAR, stock_name VARCHAR, "
        "limit_up_time VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_realtime_all_boards VALUES "
        "(?, 2, '000001', '测试', '09:35', current_timestamp)",
        [trade_date],
    )
    # Normalized view path for candidates: stock_candidate_score fallback
    con.execute(
        "CREATE TABLE stock_candidate_score("
        "trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, "
        "evidence_json VARCHAR, source VARCHAR)"
    )
    con.execute(
        "INSERT INTO stock_candidate_score VALUES (?,?,?,?,?,?)",
        [trade_date, "000001", "测试", 88, "{}", "limit_pool"],
    )
    ensure_l2_focus_checkpoint(con)
    con.close()

    payload = {
        "000001": {
            "date": trade_date,
            "data": [
                {
                    "time": "10:00",
                    "price": 10.1,
                    "avg_price": 10.0,
                    "volume": 1000,
                    "turnover": 10000,
                    "main_fund_net": 5000,
                    "date": trade_date,
                },
                {
                    "time": "10:01",
                    "price": 10.2,
                    "avg_price": 10.05,
                    "volume": 1200,
                    "turnover": 12000,
                    "main_fund_net": 6000,
                    "date": trade_date,
                },
            ],
        }
    }
    client = _FakeClient(payload)
    # Monkeypatch init_schema to no-op for this unit test (schema already created).
    import trade_system.l2_focus as mod

    mod.init_schema = lambda conn: None  # type: ignore
    # Force KPL path (probe would otherwise open a real HTTP client).
    import trade_system.l2_focus as focus_mod

    original_probe = focus_mod._probe_kpl_stock_intraday
    focus_mod._probe_kpl_stock_intraday = lambda *a, **k: True  # type: ignore

    try:
        result = collect_l2_focus(
            str(db),
            trade_date,
            max_stocks=10,
            codes=["000001"],
            total_budget_seconds=30,
            include_bigorder=False,
            client=client,  # type: ignore[arg-type]
        )
    finally:
        focus_mod._probe_kpl_stock_intraday = original_probe  # type: ignore

    assert result["status"] in {"success", "partial"}
    assert result["intraday_rows"] >= 2
    assert result["stock_codes_ok"] == 1
    assert result.get("kpl_probe_ok") is True
    assert any(ep == "/l2/stock-intraday" for ep, _ in client.calls)

    con = duckdb.connect(str(db), read_only=True)
    n, mx = con.execute(
        "SELECT count(*), max(price) FROM l2_stock_intraday "
        "WHERE CAST(date AS VARCHAR)=? AND stock_code='000001'",
        [trade_date],
    ).fetchone()
    batch = con.execute(
        "SELECT status, stock_codes_ok FROM l2_focus_batch WHERE CAST(trade_date AS VARCHAR)=?",
        [trade_date],
    ).fetchone()
    con.close()
    assert n >= 2
    assert float(mx) == 10.2
    assert batch[0] in {"success", "partial"}
    assert int(batch[1]) == 1


def test_probe_empty_skips_kpl_loop_and_uses_trends(tmp_path, monkeypatch):
    """Empty KPL probe must not iterate all candidates (cooldown storm)."""
    db = tmp_path / "l2_probe.duckdb"
    trade_date = "2026-07-31"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE l2_stock_intraday(
            date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE,
            avg_price DOUBLE, volume BIGINT, turnover BIGINT, main_fund_net BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE l2_stock_bigorder(
            date DATE, stock_code VARCHAR, time VARCHAR, big_net_amount BIGINT,
            big_buy BIGINT, big_sell BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    ensure_l2_focus_checkpoint(con)
    con.close()

    client = _FakeClient({})
    import trade_system.l2_focus as mod

    mod.init_schema = lambda conn: None  # type: ignore
    monkeypatch.setattr(mod, "_probe_kpl_stock_intraday", lambda *a, **k: False)

    def _fake_trends(code, date=None):
        return {
            "code": code,
            "trends": [
                {
                    "time": "2026-07-31 09:31",
                    "price": 10.0,
                    "avg": 10.0,
                    "volume": 1,
                    "amount": 10,
                },
            ],
        }

    monkeypatch.setattr(
        "trade_system.stock_data_sources._from_em_trends",
        _fake_trends,
    )

    result = collect_l2_focus(
        str(db),
        trade_date,
        codes=["000001", "000002", "000003"],
        include_bigorder=False,
        client=client,  # type: ignore[arg-type]
    )
    # Fake client must not be hammered for every code when probe fails.
    assert client.calls == []
    assert result["source"] == "eastmoney_trends2"
    assert result["kpl_probe_ok"] is False
    assert result["stock_codes_ok"] == 3


def test_eastmoney_trends_fallback_when_kpl_empty(tmp_path, monkeypatch):
    db = tmp_path / "l2_fb.duckdb"
    trade_date = "2026-07-31"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE l2_stock_intraday(
            date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE,
            avg_price DOUBLE, volume BIGINT, turnover BIGINT, main_fund_net BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE l2_stock_bigorder(
            date DATE, stock_code VARCHAR, time VARCHAR, big_net_amount BIGINT,
            big_buy BIGINT, big_sell BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    ensure_l2_focus_checkpoint(con)
    con.close()

    client = _FakeClient({})  # KPL always empty
    import trade_system.l2_focus as mod

    mod.init_schema = lambda conn: None  # type: ignore
    monkeypatch.setattr(mod, "_probe_kpl_stock_intraday", lambda *a, **k: False)

    def _fake_trends(code, date=None):
        return {
            "code": code,
            "name": "测试",
            "date": date,
            "trends": [
                {"time": "2026-07-31 09:31", "price": 11.0, "avg": 10.9, "volume": 100, "amount": 1100},
                {"time": "2026-07-31 09:32", "price": 11.1, "avg": 11.0, "volume": 120, "amount": 1320},
            ],
        }

    monkeypatch.setattr(
        "trade_system.stock_data_sources._from_em_trends",
        _fake_trends,
    )

    result = collect_l2_focus(
        str(db),
        trade_date,
        max_stocks=5,
        codes=["000001"],
        include_bigorder=False,
        client=client,  # type: ignore[arg-type]
    )
    assert result["source"] == "eastmoney_trends2"
    assert result["stock_codes_ok"] == 1
    assert result["intraday_rows"] >= 2
    assert client.calls == []
    con = duckdb.connect(str(db), read_only=True)
    times = [
        r[0]
        for r in con.execute(
            "SELECT time FROM l2_stock_intraday WHERE stock_code='000001' ORDER BY time"
        ).fetchall()
    ]
    con.close()
    assert times == ["09:31", "09:32"]


def test_l2_failed_refresh_preserves_previous_same_day_rows(tmp_path, monkeypatch):
    db = tmp_path / "l2_preserve.duckdb"
    trade_date = "2026-07-31"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE l2_stock_intraday(
            date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE,
            avg_price DOUBLE, volume BIGINT, turnover BIGINT, main_fund_net BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE l2_stock_bigorder(
            date DATE, stock_code VARCHAR, time VARCHAR, big_net_amount BIGINT,
            big_buy BIGINT, big_sell BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        "INSERT INTO l2_stock_intraday(date,stock_code,time,price) "
        "VALUES (?, '000001', '10:00', 10.5)",
        [trade_date],
    )
    ensure_l2_focus_checkpoint(con)
    con.close()

    import trade_system.l2_focus as mod

    mod.init_schema = lambda conn: None  # type: ignore
    monkeypatch.setattr(mod, "_probe_kpl_stock_intraday", lambda *a, **k: False)
    monkeypatch.setattr(
        "trade_system.stock_data_sources._from_em_trends",
        lambda code, date=None: {
            "code": code,
            "date": "20260730",
            "trends": [
                {
                    "time": "2026-07-30 09:31",
                    "price": 9.0,
                    "avg": 9.0,
                    "volume": 1,
                    "amount": 9,
                }
            ],
        },
    )

    result = collect_l2_focus(
        str(db),
        trade_date,
        codes=["000001"],
        include_bigorder=False,
        client=_FakeClient({}),  # type: ignore[arg-type]
    )
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute(
        "SELECT time,price FROM l2_stock_intraday "
        "WHERE CAST(date AS VARCHAR)=? AND stock_code='000001'",
        [trade_date],
    ).fetchall()
    con.close()
    assert result["status"] == "empty"
    assert rows == [("10:00", 10.5)]


def test_intraday_plan_includes_l2_focus_and_boosted_quotes():
    from scripts.run_integrated_daily import command_plan

    steps = command_plan(
        "sample.duckdb",
        "2026-07-31",
        include_collection=True,
        phase="intraday",
        signal_limit=120,
    )
    names = [s[0] for s in steps]
    assert "collect_l2_focus" in names
    assert "collect_executable_quotes" in names
    by_name = {n: cmd for n, cmd, _ in steps}
    assert "--auto-boost-if-kpl-stale" in by_name["collect_l2_focus"]
    assert "--auto-boost-if-kpl-stale" in by_name["collect_executable_quotes"]
    # Collection remains available during migration; old decisions are retired.
    assert "generate_intraday_stage_signals" not in names
