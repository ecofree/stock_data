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


def test_wrong_date_quote_is_rejected(tmp_path):
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
