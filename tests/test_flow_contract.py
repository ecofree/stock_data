from __future__ import annotations


from trade_system.flow_contract import normalize_stock_flow_row


def test_tushare_total_is_not_main_orders_net():
    row = normalize_stock_flow_row(
        {
            "net_mf_amount": 8,
            "buy_sm_amount": 1,
            "sell_sm_amount": 0,
            "buy_md_amount": 2,
            "sell_md_amount": 0,
            "buy_lg_amount": 3,
            "sell_lg_amount": 1,
            "buy_elg_amount": 4,
            "sell_elg_amount": 1,
        },
        "tushare_relay",
    )
    assert row["main_net"] == 50_000
    assert row["net_total"] == 80_000
    assert row["flow_definition"] == "main_orders_net"
    assert row["amount_unit"] == "yuan"


def test_adapter_and_contract_share_missing_native_total_and_single_conversion(monkeypatch):
    from trade_system.adapters import kline_sources
    raw = {"trade_date": "20260714", "buy_sm_amount": 1, "sell_sm_amount": 0,
           "buy_md_amount": 2, "sell_md_amount": 0, "buy_lg_amount": 3,
           "sell_lg_amount": 1, "buy_elg_amount": 4, "sell_elg_amount": 1}
    monkeypatch.setattr(kline_sources, "_xiaodefa_query", lambda *a, **k: [raw])
    adapted = kline_sources._from_xiaodefa_moneyflow("000001")[0]
    assert adapted["net_total"] is None  # Complete buckets are not the native reported total.
    assert adapted["main_net"] == 50000
    assert adapted["raw"] is raw
    again = normalize_stock_flow_row(adapted, "xiaodefa")
    assert again["main_net"] == 50000 and again["net_total"] is None
    raw["buy_lg_amount"] = float("nan")
    adapted = kline_sources._from_xiaodefa_moneyflow("000001")[0]
    assert adapted["main_net"] is None
    assert adapted["large_net"] is None
    assert adapted["super_net"] == 30000


def test_normalized_overflow_cannot_reappear_from_raw_on_second_ingestion():
    raw = {"source_api": "moneyflow", "amount_unit": "10000_yuan",
           "buy_elg_amount": 1e308, "sell_elg_amount": 0,
           "buy_lg_amount": 1, "sell_lg_amount": 0, "net_mf_amount": 1e308}
    normalized = normalize_stock_flow_row(raw, "xiaodefa")
    assert normalized["super_net"] is None and normalized["net_total"] is None
    again = normalize_stock_flow_row({**normalized, "raw": raw}, "xiaodefa")
    assert again == normalized
