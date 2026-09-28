"""Quote parsing and date qualification; legacy signal generation is retired."""

from __future__ import annotations

import duckdb

from trade_system.executable_quotes import (
    TENCENT_SPOT_PROVIDER,
    is_delayed_provider,
    parse_tencent_parts,
    quote_trade_date,
    upsert_executable_quotes,
)


def test_is_delayed_provider():
    assert is_delayed_provider("eastmoney_intraday_clist_delay") is True
    assert is_delayed_provider("eastmoney_intraday_clist") is False
    assert is_delayed_provider("tencent_spot_quote") is False


def test_quote_trade_date_requires_provider_date():
    assert quote_trade_date("20260730102800") == "2026-07-30"
    assert quote_trade_date("2026-07-30 10:28:00") == "2026-07-30"
    assert quote_trade_date("10:28:00") is None


def test_parse_tencent_parts_extracts_price_and_book():
    parts = [""] * 50
    parts[1] = "测试"
    parts[2] = "000001"
    parts[3] = "10.5"
    parts[4] = "10.0"
    parts[9] = "10.4"
    parts[10] = "100"
    parts[19] = "10.6"
    parts[20] = "200"
    parts[30] = "100000"
    parts[32] = "5.0"
    parsed = parse_tencent_parts(parts)
    assert parsed is not None
    assert parsed["stock_code"] == "000001"
    assert parsed["price"] == 10.5
    assert parsed["ask1"] == 10.6
    assert parsed["provider"] == TENCENT_SPOT_PROVIDER


def test_wrong_date_quote_is_rejected(tmp_path, monkeypatch, capsys):
    db = tmp_path / "wrong_date.duckdb"
    con = duckdb.connect(str(db))
    written = upsert_executable_quotes(
        con,
        "2026-07-30",
        {
            "000001": {
                "price": 10.5,
                "ask1": 10.6,
                "ask1_vol": 100,
                "provider": TENCENT_SPOT_PROVIDER,
                "quote_time": "20260729150000",
            }
        },
    )
    assert written == 0
    assert con.execute("SELECT count(*) FROM executable_quote_snapshot").fetchone()[0] == 0
    con.close()
    import json
    import pytest
    from trade_system import executable_quotes as quotes
    from trade_system.collection_profiles import read_product_counts
    context = {'demand_id': 'quote-commit-test'}
    monkeypatch.setenv('STOCKDATA_REQUEST_CONTEXT', json.dumps(context))
    incoming = {'000001': {'price': 10, 'quote_time': '20260730150000'},
                '000002': {'price': 'invalid-price', 'quote_time': '20260730150000'}}
    with pytest.raises(duckdb.ConversionException):
        quotes.collect_executable_quotes(db, '2026-07-30', codes=list(incoming), fetcher=lambda _: incoming)
    counts = read_product_counts(capsys.readouterr().out, context)['scopes']['executable_quote_snapshot']
    assert (counts['rows_parsed'], counts['rows_written']) == (2, 0)
    with duckdb.connect(str(db)) as check:
        assert check.execute('SELECT count(*) FROM executable_quote_snapshot').fetchone()[0] == 0
    incoming['000002'] = {'price': 12, 'quote_time': '20260729150000'}
    result = quotes.collect_executable_quotes(db, '2026-07-30', codes=list(incoming), fetcher=lambda _: incoming)
    assert result['status'] == 'partial_invalid_timestamp'
    counts = read_product_counts(capsys.readouterr().out, context)['scopes']['executable_quote_snapshot']
    assert (counts['rows_parsed'], counts['rows_written']) == (2, 1)


def test_quote_universe_uses_manual_state_and_ignores_old_scores(tmp_path):
    from trade_system.executable_quotes import candidate_codes_for_quotes
    with duckdb.connect(str(tmp_path / "universe.duckdb")) as con:
        con.execute("CREATE TABLE stock_candidate_score(trade_date DATE,stock_code VARCHAR,score DOUBLE)")
        con.execute("INSERT INTO stock_candidate_score VALUES ('2026-09-18','600000',99)")
        assert candidate_codes_for_quotes(con, '2026-09-18') == []
        con.execute("CREATE TABLE holdings(stock_code VARCHAR,status VARCHAR,shares INTEGER,entry_date DATE,exit_date DATE)")
        con.execute("INSERT INTO holdings VALUES ('000001','open',100,'2026-09-17',NULL),"
                    "('000002','closed',0,'2026-09-17','2026-09-18')")
        con.execute("CREATE TABLE watchlist(trade_date DATE,stock_code VARCHAR,status VARCHAR,priority INTEGER,created_at TIMESTAMP)")
        con.execute("INSERT INTO watchlist VALUES ('2026-09-17','000003','active',1,current_timestamp),"
                    "('2026-09-18','000003','removed',1,current_timestamp),"
                    "('2026-09-18','000004','active',1,current_timestamp),"
                    "('2026-09-19','000005','active',1,current_timestamp)")
        assert candidate_codes_for_quotes(con, '2026-09-18') == ['000001', '000004']
        assert candidate_codes_for_quotes(con, '2026-09-18', limit=1) == ['000001']
