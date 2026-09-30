from __future__ import annotations


from trade_system.flow_contract import normalize_stock_flow_row


def test_tushare_total_is_not_main_orders_net():
    import duckdb
    from trade_system.flow_contract import independent_comparison_contract
    with duckdb.connect(':memory:') as con:
        con.execute('''CREATE TABLE multi_source_stock_flow (source_date DATE,provider VARCHAR,
            origin_provider VARCHAR,source_api VARCHAR,flow_definition VARCHAR,amount_unit VARCHAR,
            field_mapping_version VARCHAR,main_net DOUBLE,is_stale BOOLEAN)''')
        con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-09-24','relay','eastmoney',"
                    "'moneyflow_dc','provider_main_orders_net','yuan','v3',1,false),"
                    "('2026-09-24','direct','eastmoney','dc','provider_main_orders_net','yuan','v3',1,false)")
        assert independent_comparison_contract(con,'2026-09-24','relay','direct')['reason'] == 'same_original_source'
        con.execute("UPDATE multi_source_stock_flow SET origin_provider='tushare',flow_definition='main_orders_net' WHERE provider='direct'")
        assert independent_comparison_contract(con,'2026-09-24','relay','direct')['reason'] == 'definition_alignment_unproven'
        con.execute("UPDATE multi_source_stock_flow SET flow_definition='provider_main_orders_net' WHERE provider='direct'")
        assert independent_comparison_contract(con,'2026-09-24','relay','direct')['reason'] == 'definition_evidence_missing'
        # A shared label is not evidence of common order grouping or buckets.
        from unittest.mock import patch
        import trade_system.flow_contract as contract
        key = tuple(sorted([('eastmoney','moneyflow_dc','provider_main_orders_net','v3'),
                            ('tushare','dc','provider_main_orders_net','v3')]))
        evidence = dict(canonical_definition='fixture_active_order_buckets',
                        source_specification_sha256=['a'*64,'b'*64],
                        valid_from='2026-09-24',valid_through='2026-09-24')
        with patch.dict(contract.VERIFIED_FLOW_COMPARISONS,{key:evidence}):
            assert not independent_comparison_contract(con,'2026-09-24','relay','direct')['eligible']
            semantics = dict(order_grouping='original_order', trade_side='aggressor',
                             size_buckets='CNY:[200000,1000000),[1000000,infinity)',
                             session='09:15-15:00 Shanghai auction included',
                             security_scope='same listed A-share population', net_formula='large_buy-large_sell')
            evidence['specifications'] = [semantics.copy(), semantics.copy()]
            assert independent_comparison_contract(con,'2026-09-24','relay','direct')['eligible']
            evidence['specifications'][1]['order_grouping'] = 'trade_print'
            assert independent_comparison_contract(con,'2026-09-24','relay','direct')['missing_definition_evidence'] == ['order_grouping']
            evidence['specifications'][1]['order_grouping'] = 'original_order'
            evidence['valid_through'] = 'not-a-date'
            assert not independent_comparison_contract(con,'2026-09-24','relay','direct')['eligible']
            evidence['valid_through'] = '2026-09-24'
            evidence['source_specification_sha256'] = ['unknown','b'*64]
            assert not independent_comparison_contract(con,'2026-09-24','relay','direct')['eligible']
        # Different vendor labels may map only via an explicitly reviewed,
        # fully specified pair. Identical strings alone never authorize it.
        con.execute("UPDATE multi_source_stock_flow SET flow_definition='other_vendor_main' WHERE provider='direct'")
        other_key = tuple(sorted([key[0], ('tushare','dc','other_vendor_main','v3')]))
        evidence['source_specification_sha256'] = ['a'*64, 'b'*64]
        with patch.dict(contract.VERIFIED_FLOW_COMPARISONS,{other_key:evidence}):
            assert independent_comparison_contract(con,'2026-09-24','relay','direct')['eligible']
        con.execute("UPDATE multi_source_stock_flow SET main_net=NULL WHERE provider='direct'")
        assert not independent_comparison_contract(con,'2026-09-24','relay','direct')['eligible']
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


def test_independent_amount_gate_rejects_unit_scale_and_shrunk_denominator(tmp_path, monkeypatch):
    import duckdb
    from trade_system.schema import init_schema
    import trade_system.flow_contract as contract
    from scripts import reconcile_independent_stock_flow as entry
    import scripts.collect_intraday_stock_flow_market as market
    db = tmp_path/'amount.duckdb'
    with duckdb.connect(str(db)) as con:
        init_schema(con)
        for provider,origin,api in [('primary','eastmoney','moneyflow_dc'),('reference','tushare','moneyflow')]:
            for code,amount in [('000001',10000),('600000',-20000)]:
                con.execute('''INSERT INTO multi_source_stock_flow(source_date,stock_code,provider,origin_provider,
                    source_api,flow_definition,amount_unit,field_mapping_version,main_net,is_stale,fetched_at)
                    VALUES ('2026-07-09',?,?,?,?, 'fixture_definition','yuan','v3',?,false,'2026-07-09 17:00:00')''',
                    [code,provider,origin,api,amount])
    monkeypatch.setattr(market,'_a_share_universe_by_exchange',lambda *args:{'000001':'SZ','600000':'SH'})
    semantics = {k:'fixture_identical' for k in contract.FLOW_DEFINITION_AXES}
    key = tuple(sorted([('eastmoney','moneyflow_dc','fixture_definition','v3'),('tushare','moneyflow','fixture_definition','v3')]))
    proof = dict(canonical_definition='fixture',source_specification_sha256=['a'*64,'b'*64],
        valid_from='2026-07-09',valid_through='2026-07-09',specifications=[semantics,semantics],
        amount_precision=dict(primary_quantum_yuan=100,reference_quantum_yuan=100))
    monkeypatch.setitem(contract.VERIFIED_FLOW_COMPARISONS,key,proof)
    assert entry.reconcile(db,'2026-07-09',primary_provider='primary',reference_provider='reference')['status'] == 'pass'
    with duckdb.connect(str(db)) as con:
        con.execute("UPDATE multi_source_stock_flow SET main_net=main_net*10000 WHERE provider='primary'")
    result = entry.reconcile(db,'2026-07-09',primary_provider='primary',reference_provider='reference')
    assert result['correlation_main_net'] == 1 and result['sign_agreement_pct'] == 100
    assert result['status'] == 'warning' and result['amount_match_pct'] == 0
    with duckdb.connect(str(db)) as con:
        con.execute("UPDATE multi_source_stock_flow SET main_net=main_net/10000 WHERE provider='primary'")
    monkeypatch.setattr(market,'_a_share_universe_by_exchange',lambda *args:{'000001':'SZ','600000':'SH','600001':'SH'})
    result = entry.reconcile(db,'2026-07-09',primary_provider='primary',reference_provider='reference')
    assert result['status'] == 'warning' and result['expected_rows'] == 3
    assert result['overlap_reference_pct'] < 67


def test_bj_tushare_moneyflow_zero_is_not_proof_of_field_capability():
    row = dict(ts_code='920128.BJ',source_api='moneyflow',amount_unit='10000_yuan',
               buy_lg_amount=0,sell_lg_amount=0,buy_elg_amount=0,sell_elg_amount=0,net_mf_amount=0)
    result = normalize_stock_flow_row(row,'tushare')
    assert result['main_net'] is None and result['net_total'] is None
    assert result['flow_definition'] == 'product_security_scope_unverified'
