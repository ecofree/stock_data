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


def _candidate_bytes(codes=('000001.SZ', '600000.SH', '920128.BJ')):
    """Synthetic protocol fixture, never labelled a supplier market receipt."""
    import json
    from trade_system.flow_contract import GANGTISE_FLOW_FIELDS
    fields = ['securityCode', 'tradeDate', *GANGTISE_FLOW_FIELDS]
    buckets = [(1, 3, -2), (2, 1, 1), (5, 2, 3), (7, 3, 4), (15, 9, 6), (12, 5, 7)]
    return json.dumps({'code': '000000', 'status': True, 'data': {
        'fieldList': fields, 'list': [[c, '20260929', *(v for bucket in buckets for v in bucket)]
                                    for c in codes]}}).encode()


def test_candidate_scope_units_arithmetic_and_original_time_do_not_authorize_independence():
    from trade_system.flow_contract import parse_candidate_flow_response
    codes = ['000001.SZ', '600000.SH', '920128.BJ']
    arrival = '2026-09-29T17:00:00+08:00'
    result = parse_candidate_flow_response(_candidate_bytes(), '2026-09-29', codes, received_at=arrival)
    assert result['returned_scope_complete'] and result['arithmetic_qualified']
    assert result['rows'][2]['security_code'] == '920128.BJ'
    assert all(r['received_at'] == arrival and r['candidate_main_net_yuan'] == '7' for r in result['rows'])
    assert result['origin_provider'] == 'unknown' and not result['independent_comparison_eligible']
    assert result['canonical_writes'] == result['production_writes'] == 0
    partial = parse_candidate_flow_response(_candidate_bytes(codes[:2]), '2026-09-29', codes, received_at=arrival)
    assert partial['missing_securities'] == ['920128.BJ'] and not partial['returned_scope_complete']


def test_candidate_rejects_date_fallback_width_duplicates_nonfinite_and_preserves_null():
    import json
    import pytest
    from copy import deepcopy
    from trade_system.flow_contract import parse_candidate_flow_response
    raw = json.loads(_candidate_bytes())
    kwargs = dict(trade_date='2026-09-29', securities=['000001.SZ', '600000.SH', '920128.BJ'],
                  received_at='2026-09-29T17:00:00+08:00')
    for mutation in ('date', 'width', 'duplicate', 'scope', 'field', 'nonfinite', 'boolean'):
        payload = deepcopy(raw)
        data = payload['data']
        if mutation == 'date':
            data['list'][0][1] = '2026-09-28'
        elif mutation == 'width':
            data['list'][0].pop()
        elif mutation == 'duplicate':
            data['list'][1][0] = data['list'][0][0]
        elif mutation == 'scope':
            data['list'][2][0] = '920000.BJ'
        elif mutation == 'field':
            data['fieldList'][3] = data['fieldList'][2]
        elif mutation == 'nonfinite':
            data['list'][0][2] = float('nan')
        else:
            data['list'][0][2] = True
        with pytest.raises(ValueError):
            parse_candidate_flow_response(json.dumps(payload).encode(), **kwargs)
    with pytest.raises(ValueError, match='duplicate response'):
        parse_candidate_flow_response(b'{"code":"000000","code":"000000"}', **kwargs)
    with pytest.raises(ValueError, match='timezone'):
        parse_candidate_flow_response(_candidate_bytes(), **dict(kwargs, received_at='2026-09-29T17:00:00'))
    raw['data']['list'][0][2] = None
    result = parse_candidate_flow_response(json.dumps(raw).encode(), **kwargs)
    assert not result['arithmetic_qualified']
    assert 'missing_amount:smallInflow' in result['rows'][0]['quality_issues']
    raw['data']['list'][0][2] = -1
    raw['data']['list'][0][-1] = 999
    result = parse_candidate_flow_response(json.dumps(raw).encode(), **kwargs)
    assert any('negative_gross' in v for v in result['rows'][0]['quality_issues'])
    assert 'main_bucket_arithmetic:NetInflow' in result['rows'][0]['quality_issues']


def test_candidate_request_uses_shared_wire_budget_and_stops_rejected_response(monkeypatch):
    import json
    import subprocess
    import pytest
    from types import SimpleNamespace
    from trade_system.flow_contract import request_candidate_flow
    from trade_system.http_transport import diagnostic_budget
    calls = []
    rejected = False

    def run(command, *, input, timeout, **kwargs):
        wire = json.loads(input)
        assert 'fixture-secret' not in ' '.join(command)
        assert wire['headers']['Authorization'] == 'Bearer fixture-secret'
        request = json.loads(wire['body'])
        assert request['startDate'] == request['endDate'] == '2026-09-29'
        assert len(request['securityList']) == request['limit'] == 1
        assert 0 < timeout <= 20
        calls.append(wire)
        raw = (b'{"code":"8000014","status":false,"msg":"fixture-secret"}' if rejected
               else _candidate_bytes(request['securityList']))
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true}\n' + raw)

    monkeypatch.setattr(subprocess, 'run', run)
    kwargs = dict(authorization='fixture-secret', entitlement_sha256='a'*64)
    with pytest.raises(RuntimeError, match='diagnostic context'):
        request_candidate_flow('2026-09-29', ['000001.SZ'], **kwargs)
    assert not calls
    with diagnostic_budget() as budget:
        original, result = request_candidate_flow('2026-09-29', ['000001.SZ'], **kwargs)
        assert original == _candidate_bytes(['000001.SZ']) and not result['independent_comparison_eligible']
        reused_raw, reused = request_candidate_flow('2026-09-29', ['000001.SZ'], **kwargs)
        assert reused_raw == original and reused['reused'] and reused['received_at'] == result['received_at']
        assert budget['attempts'] == len(calls) == 1
        request_candidate_flow('2026-09-29', ['600000.SH'], **kwargs)
        with pytest.raises(RuntimeError, match='budget'):
            request_candidate_flow('2026-09-29', ['920128.BJ'], **kwargs)
        assert len(calls) == budget['attempts'] == 2
    rejected = True
    with diagnostic_budget() as budget:
        original, result = request_candidate_flow('2026-09-29', ['000001.SZ'], **kwargs)
        assert b'8000014' in original and result['status'] == 'response_rejected'
        assert 'fixture-secret' not in json.dumps(result)
        assert budget['stopped'] == 'business_rejected'
        with pytest.raises(RuntimeError, match='stopped'):
            request_candidate_flow('2026-09-29', ['000001.SZ'], **kwargs)
        assert budget['attempts'] == 1


def test_candidate_file_hash_and_configuration_presence_are_not_entitlement(tmp_path):
    import hashlib
    import pytest
    from scripts.audit_stock_flow_contract import audit_candidate_file
    from trade_system.flow_contract import candidate_flow_capabilities, candidate_flow_request
    path = tmp_path / 'original.json'
    raw = _candidate_bytes(['000001.SZ'])
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    result = audit_candidate_file(path, digest, '2026-09-29', ['000001.SZ'], '2026-09-29T17:00:00+08:00')
    assert result['response_sha256'] == digest
    path.write_bytes(raw + b' ')
    with pytest.raises(ValueError, match='SHA256 differs'):
        audit_candidate_file(path, digest, '2026-09-29', ['000001.SZ'], '2026-09-29T17:00:00+08:00')
    capabilities = candidate_flow_capabilities({'HITHINK_FINANCE_API_KEY': 'fixture-secret'})
    assert capabilities['hithink_ai_client_key_present']
    assert not capabilities['hithink_ai_key_authorizes_ifind'] and not capabilities['entitlement_verified']
    official_keys = {'GTS_ACCESS_KEY': 'fixture-access', 'GTS_SECRET_KEY': 'fixture-secret'}
    assert candidate_flow_capabilities(official_keys)['gangtise_ak_sk_present']
    assert not candidate_flow_capabilities({'GTS_ACCESS_KEY': 'fixture-access'})['gangtise_ak_sk_present']
    assert not candidate_flow_capabilities({'GANGTISE_AK': 'fixture-access',
                                          'GANGTISE_SK': 'fixture-secret'})['gangtise_ak_sk_present']
    assert not candidate_flow_capabilities(official_keys)['entitlement_verified']
    for codes in (['aShares'], ['920128'], ['000001.SZ', '000001.SZ']):
        with pytest.raises(ValueError):
            candidate_flow_request('2026-09-29', codes)


def test_candidate_cli_never_opens_database_and_refuses_output_escape_or_overwrite(tmp_path, monkeypatch):
    import json
    import sys
    import pytest
    from scripts import audit_stock_flow_contract as entry
    monkeypatch.setattr(entry, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(entry.duckdb, 'connect', lambda *a, **k: pytest.fail('candidate opened database'))
    target = tmp_path / 'reports' / 'candidate.json'
    base = ['audit', '--candidate-plan', '--date', '2026-09-29', '--codes', '000001.SZ',
            '--db', 'production-path-must-not-open.duckdb', '--out']
    monkeypatch.setattr(sys, 'argv', base + [str(target)])
    assert entry.main() == 0
    result = json.loads(target.read_text(encoding='utf-8'))
    assert result['authenticated_requests'] == result['production_writes'] == 0
    assert not result['independent_comparison_eligible']
    original = target.read_bytes()
    with pytest.raises(SystemExit):
        entry.main()
    assert target.read_bytes() == original
    monkeypatch.setattr(sys, 'argv', base + [str(tmp_path / 'escape.json')])
    with pytest.raises(SystemExit):
        entry.main()
    assert not (tmp_path / 'escape.json').exists()
