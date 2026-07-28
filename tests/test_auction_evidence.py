import duckdb

from schema import init_schema
from scripts.collect_auction_evidence import (
    _codes,
    _collect_tencent_auction_quotes,
    _ensure_batch,
)

from trade_system.auction_evidence import (
    build_auction_evidence_snapshot,
    ensure_auction_evidence_tables,
    persist_auction_evidence_snapshot,
    render_auction_evidence_report,
    resolve_auction_trade_date,
)


def test_auction_evidence_prefers_tick_over_fallback_sources(tmp_path):
    db_path = tmp_path / "auction.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE auction_tick(date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE, volume BIGINT)")
    con.execute("INSERT INTO auction_tick VALUES ('2026-07-08','000001','09:24:30',10.0,1000)")
    con.execute("CREATE TABLE auction_bidding_anomaly(date DATE, stock_code VARCHAR, anomaly_type VARCHAR, anomaly_value DOUBLE)")
    con.execute("INSERT INTO auction_bidding_anomaly VALUES ('2026-07-08','000001','bidding_amount',888)")
    con.close()

    rows = build_auction_evidence_snapshot(db_path, trade_date="2026-07-08")

    assert rows[0]["stock_code"] == "000001"
    assert rows[0]["source_table"] == "auction_tick"
    assert rows[0]["confirmation"] == "tick_confirmed"
    assert rows[0]["is_fallback"] is False
    assert rows[0]["tick_rows"] == 1


def test_auction_evidence_uses_anomaly_when_tick_is_empty(tmp_path):
    db_path = tmp_path / "auction.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE auction_tick(date DATE, stock_code VARCHAR, time VARCHAR, price DOUBLE, volume BIGINT)")
    con.execute("CREATE TABLE auction_bidding_anomaly(date DATE, stock_code VARCHAR, anomaly_type VARCHAR, anomaly_value DOUBLE)")
    con.execute("INSERT INTO auction_bidding_anomaly VALUES ('2026-07-08','000002','bidding_amount',1234)")
    con.close()

    rows = build_auction_evidence_snapshot(db_path, trade_date="2026-07-08")

    assert rows[0]["stock_code"] == "000002"
    assert rows[0]["source_table"] == "auction_bidding_anomaly"
    assert rows[0]["confirmation"] == "anomaly_confirmed"
    assert rows[0]["is_fallback"] is True
    assert "auction_tick_missing" in rows[0]["missing_reason"]


def test_tencent_order_book_snapshot_is_kept_separate_from_trade_ticks(tmp_path):
    db_path = tmp_path / "auction-quote.duckdb"
    parts = [""] * 50
    parts[1] = "Test"
    parts[2] = "000001"
    parts[3] = "10.20"
    parts[4] = "10.00"
    parts[5] = "10.20"
    parts[6] = "1234"
    parts[9], parts[10] = "10.19", "500"
    parts[19], parts[20] = "10.20", "200"
    parts[30] = "20260727092510"
    con = duckdb.connect(str(db_path))
    init_schema(con)
    _ensure_batch(con)
    written = _collect_tencent_auction_quotes(
        con,
        "2026-07-27",
        ["000001"],
        quote_fetcher=lambda _codes: {"000001": parts},
    )
    con.commit()
    assert written == 1
    assert con.execute("SELECT count(*) FROM auction_tick").fetchone()[0] == 0
    assert con.execute(
        "SELECT quote_time,provider,order_imbalance "
        "FROM auction_quote_snapshot"
    ).fetchone() == ("09:25:10", "tencent_qt", 3 / 7)
    con.close()

    rows = build_auction_evidence_snapshot(db_path, "2026-07-27")
    assert rows[0]["source_table"] == "auction_quote_snapshot"
    assert rows[0]["confirmation"] == "quote_confirmed"
    assert rows[0]["is_fallback"] is False
    assert "not_trade_tick" in rows[0]["evidence_json"]


def test_tencent_order_book_rejects_non_auction_source_timestamp(tmp_path):
    db_path = tmp_path / "auction-quote-time.duckdb"
    parts = [""] * 50
    parts[2], parts[3], parts[30] = "000001", "10.2", "20260727093001"
    con = duckdb.connect(str(db_path))
    _ensure_batch(con)
    assert _collect_tencent_auction_quotes(
        con,
        "2026-07-27",
        ["000001"],
        quote_fetcher=lambda _codes: {"000001": parts},
    ) == 0
    con.close()


def test_auction_evidence_preserves_market_summary_as_market_level_fallback(tmp_path):
    db_path = tmp_path / "auction.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE advanced_morning_bidding_summary(
            date DATE, total_amount BIGINT, limit_up_count INTEGER, limit_down_count INTEGER
        )
        """
    )
    con.execute("INSERT INTO advanced_morning_bidding_summary VALUES ('2026-07-08',100000000,12,3)")
    con.close()

    rows = build_auction_evidence_snapshot(db_path, trade_date="2026-07-08")

    assert rows[0]["stock_code"] is None
    assert rows[0]["source_table"] == "advanced_morning_bidding_summary"
    assert rows[0]["confirmation"] == "market_confirmed"
    assert rows[0]["is_fallback"] is True


def test_auction_evidence_persists_snapshot_and_renders_report(tmp_path):
    db_path = tmp_path / "auction.duckdb"
    rows = [
        {
            "trade_date": "2026-07-08",
            "stock_code": "000002",
            "source_table": "auction_bidding_anomaly",
            "confirmation": "anomaly_confirmed",
            "auction_strength": 12.34,
            "auction_amount": None,
            "tick_rows": 0,
            "is_fallback": True,
            "missing_reason": "auction_tick_missing",
            "evidence_json": "{}",
        }
    ]

    ensure_auction_evidence_tables(db_path)
    assert persist_auction_evidence_snapshot(db_path, rows) == 1
    assert persist_auction_evidence_snapshot(db_path, rows) == 1
    report = render_auction_evidence_report({"rows": rows, "counts": {"auction_tick": 0}, "gaps": ["auction_tick_missing"]})

    assert "# 竞价证据链报告" in report
    assert "auction_tick_missing" in report
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM auction_evidence_snapshot").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM v_auction_evidence").fetchone()[0] == 1
    finally:
        con.close()


def test_resolve_auction_trade_date_uses_latest_available_source_date(tmp_path):
    db_path = tmp_path / "auction_dates.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE auction_bidding_anomaly(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO auction_bidding_anomaly VALUES ('2026-07-07','000001')")
    con.execute("CREATE TABLE advanced_morning_bidding_summary(date DATE)")
    con.execute("INSERT INTO advanced_morning_bidding_summary VALUES ('2026-07-06')")
    con.close()

    assert resolve_auction_trade_date(db_path, None) == "2026-07-07"
    assert resolve_auction_trade_date(db_path, "2026-07-08") == "2026-07-08"


def test_auction_code_selection_reuses_open_connection(tmp_path):
    db_path = tmp_path / "auction_codes.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE v_limit_pool(trade_date DATE, stock_code VARCHAR, board_level INTEGER)"
    )
    con.execute(
        "INSERT INTO v_limit_pool VALUES ('2026-07-21','000001',3),('2026-07-21','600000',2)"
    )
    assert _codes(str(db_path), "2026-07-21", 1, con=con) == ["000001"]
    con.close()
