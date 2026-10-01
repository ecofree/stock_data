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


def _sina_fixture(tmp_path, monkeypatch):
    """Synthetic document/response bytes; CI never fetches official sources."""
    import hashlib
    import json
    import trade_system.flow_contract as contract
    documents, paths, specifications = {}, {}, {}
    for name, (url, _) in contract.SINA_PRODUCT_DOCUMENTS.items():
        documents[name] = ('synthetic source: ' + name).encode()
        paths[name] = tmp_path / (name + '.source')
        paths[name].write_bytes(documents[name])
        specifications[name] = (url, hashlib.sha256(documents[name]).hexdigest())
    monkeypatch.setattr(contract, 'SINA_PRODUCT_DOCUMENTS', specifications)
    row = dict(opendate='2026-09-29', trade='11.35', changeratio='0.1', turnover='0.2',
               ratioamount='0.3', netamount='36992108.7300', r0_net='68661768.3700',
               r1_net='-26419159.1700', r2_net='-4436695.7500', r3_net='-813804.7200',
               r0='400000000.0100', r1='200000000.0000', r2='100000000.0000', r3='80000000.0000')
    kwargs = dict(trade_date='2026-09-29', securities=['000001.SZ'],
                  received_at='2026-09-30T09:45:00.748385+00:00',
                  request_url=contract.SINA_FLOW_URL + '?page=1&num=3&sort=opendate&asc=0&daima=sz000001',
                  source_documents=documents)
    return row, json.dumps([row]).encode(), kwargs, paths


def test_sina_pc_net_units_and_main_keep_precision_without_canonical_promotion(tmp_path, monkeypatch):
    import hashlib
    from trade_system.flow_contract import parse_sina_flow_response, VERIFIED_FLOW_COMPARISONS
    _, raw, kwargs, _ = _sina_fixture(tmp_path, monkeypatch)
    result = parse_sina_flow_response(raw, **kwargs)
    row = result['rows'][0]
    assert row['candidate_main_net_yuan'] == '68661768.3700'  # PC r0, not r0+r1 or display rounding.
    assert row['net_amounts_yuan']['netamount'] == '36992108.7300'  # No second 10000 multiplier.
    assert result['bucket_labels']['r2_net'] == 'small' and result['bucket_labels']['r3_net'] == 'retail'
    assert result['documented_net_field_unit'] == 'yuan' and row['gross_amount_unit'] == 'unknown'
    assert row['gross_raw']['r0'] == '400000000.0100'
    assert result['response_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['request_sha256'] == hashlib.sha256(kwargs['request_url'].encode()).hexdigest()
    assert row['received_at'] == kwargs['received_at'] and row['source_date'] == kwargs['trade_date']
    assert row['identity_binding'] == 'original_request_only' and not row['response_security_identity_verified']
    assert result['net_arithmetic_qualified'] and result['arithmetic_qualified']
    assert result['canonical_writes'] == result['production_writes'] == result['new_market_requests'] == 0
    assert not result['independent_comparison_eligible'] and not result['full_sh_sz_bj_scope_verified']
    assert not result['six_axis_definition_verified'] and not VERIFIED_FLOW_COMPARISONS
    auxiliary = result['auxiliary_research']
    assert auxiliary['eligible'] and auxiliary['observed_security_codes'] == ['000001.SZ']
    assert auxiliary['original_received_at'] == kwargs['received_at']
    assert not any(auxiliary[key] for key in ('gross_fields_eligible', 'same_definition_comparison_eligible',
        'global_scope_complete', 'canonical_promotion_allowed', 'p0_or_five_day_certification'))
    axes = result['definition_evidence_by_axis']
    assert 'neutral' in axes['trade_side']['documented_product_statement']
    assert '200000' in axes['size_buckets']['documented_product_statement']
    assert axes['net_formula']['current_response_binding'] == 'reported_net_arithmetic_checked'
    assert all(axis['cross_source_alignment'] == 'unverified' for axis in axes.values())
    assert 'trade_side' not in result['missing_evidence']
    assert 'cross_source_alignment:trade_side' in result['missing_evidence']


def test_sina_rejects_source_hash_response_identity_query_and_date_fallback(tmp_path, monkeypatch):
    import json
    import pytest
    from trade_system.flow_contract import parse_sina_flow_response
    item, raw, kwargs, _ = _sina_fixture(tmp_path, monkeypatch)
    for name in ('page', 'fields', 'method'):
        documents = dict(kwargs['source_documents'])
        documents[name] += b' '
        with pytest.raises(ValueError, match='source document SHA256'):
            parse_sina_flow_response(raw, **dict(kwargs, source_documents=documents))
    for replacement in ('daima=sh600000', 'page=2', 'num=4', 'sort=r0_net', 'asc=1'):
        key = replacement.split('=')[0]
        original = next(p for p in kwargs['request_url'].split('?')[1].split('&') if p.startswith(key + '='))
        with pytest.raises(ValueError, match='scope/query'):
            parse_sina_flow_response(raw, **dict(kwargs, request_url=kwargs['request_url'].replace(original, replacement)))
    for url in (kwargs['request_url'] + '&num=3', kwargs['request_url'] + '&token=secret',
                kwargs['request_url'] + '#fragment', kwargs['request_url'].replace('https:', 'http:')):
        with pytest.raises(ValueError):
            parse_sina_flow_response(raw, **dict(kwargs, request_url=url))
    with pytest.raises(ValueError, match='BJ protocol unverified'):
        parse_sina_flow_response(raw, **dict(kwargs, securities=['920128.BJ']))
    with pytest.raises(ValueError, match='target date missing'):
        parse_sina_flow_response(json.dumps([dict(item, opendate='2026-09-28')]).encode(), **kwargs)
    for payload in ([item, item], [dict(item, opendate='2026-10-01')], [dict(item, ts_code='600000.SH')]):
        with pytest.raises(ValueError):
            parse_sina_flow_response(json.dumps(payload).encode(), **kwargs)
    with pytest.raises(ValueError, match='timezone'):
        parse_sina_flow_response(raw, **dict(kwargs, received_at='2026-09-30T17:45:00'))


def test_sina_null_mismatch_numeric_json_and_precision_fail_closed(tmp_path, monkeypatch):
    import json
    import pytest
    from trade_system.flow_contract import parse_sina_flow_response
    item, raw, kwargs, _ = _sina_fixture(tmp_path, monkeypatch)
    result = parse_sina_flow_response(json.dumps([dict(item, r0_net=None)]).encode(), **kwargs)
    assert result['rows'][0]['candidate_main_net_yuan'] is None and not result['net_arithmetic_qualified']
    assert not result['auxiliary_research']['eligible']
    assert 'missing_value:r0_net' in result['rows'][0]['quality_issues']
    result = parse_sina_flow_response(json.dumps([dict(item, netamount='0')]).encode(), **kwargs)
    assert 'net_bucket_sum_differs' in result['rows'][0]['quality_issues']
    assert not result['arithmetic_qualified'] and not result['independent_comparison_eligible']
    assert not result['auxiliary_research']['eligible']
    result = parse_sina_flow_response(json.dumps([dict(item, r0=None)]).encode(), **kwargs)
    assert result['rows'][0]['gross_raw']['r0'] is None and result['net_arithmetic_qualified']
    assert result['auxiliary_research']['eligible'] and not result['rows'][0]['gross_auxiliary_eligible']
    result = parse_sina_flow_response(json.dumps([dict(item, r0='-1')]).encode(), **kwargs)
    assert result['auxiliary_research']['eligible'] and not result['arithmetic_qualified']
    assert 'negative_gross' in result['rows'][0]['quality_issues']
    for invalid in ('NaN', 'Infinity', '1e1000', '1e-1000', True, {}, ' ', 'bad'):
        with pytest.raises(ValueError):
            parse_sina_flow_response(json.dumps([dict(item, r0_net=invalid)]).encode(), **kwargs)
    for payload in (raw.replace(b'"r0_net":', b'"netamount":', 1),
                    raw.replace(b'"36992108.7300"', b'NaN', 1), b'[]', b'{}'):
        with pytest.raises(ValueError):
            parse_sina_flow_response(payload, **kwargs)
    # Decimal JSON numbers must not pass through binary floating-point rounding.
    numeric = raw.replace(b'"68661768.3700"', b'68661768.3700')
    assert parse_sina_flow_response(numeric, **kwargs)['rows'][0]['candidate_main_net_yuan'] == '68661768.3700'


def test_sina_file_audit_and_cli_require_hashes_and_never_open_db_or_network(tmp_path, monkeypatch):
    import hashlib
    import json
    import sys
    import pytest
    import trade_system.http_transport as transport
    from scripts import audit_stock_flow_contract as entry
    _, raw, kwargs, paths = _sina_fixture(tmp_path, monkeypatch)
    receipt = tmp_path / 'retained.response'
    receipt.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(entry, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(entry.duckdb, 'connect', lambda *a, **k: pytest.fail('Sina candidate opened DB'))
    monkeypatch.setattr(transport, 'read_verified_once', lambda *a, **k: pytest.fail('Sina audit sent a request'))
    target = tmp_path / 'reports' / 'sina.json'
    args = ['audit', '--candidate-receipt', str(receipt), '--candidate-provider', 'sina',
            '--response-sha256', digest, '--date', kwargs['trade_date'], '--codes', '000001.SZ',
            '--received-at', kwargs['received_at'], '--request-url', kwargs['request_url'],
            '--db', 'production-must-not-open.duckdb']
    for name, path in paths.items():
        args += ['--sina-' + name + '-source', str(path)]
    monkeypatch.setattr(sys, 'argv', args + ['--out', str(target)])
    assert entry.main() == 0
    result = json.loads(target.read_text(encoding='utf-8'))
    assert result['rows'][0]['candidate_main_net_yuan'] == '68661768.3700'
    original = target.read_bytes()
    with pytest.raises(SystemExit):
        entry.main()
    assert target.read_bytes() == original
    monkeypatch.setattr(sys, 'argv', args + ['--out', str(tmp_path / 'escape.json')])
    with pytest.raises(SystemExit):
        entry.main()
    assert not (tmp_path / 'escape.json').exists()
    receipt.write_bytes(raw + b' ')
    with pytest.raises(ValueError, match='response SHA256 differs'):
        entry.audit_candidate_file(receipt, digest, kwargs['trade_date'], ['000001.SZ'],
                                   kwargs['received_at'], provider='sina',
                                   request_url=kwargs['request_url'], source_document_paths=paths)
    with pytest.raises(ValueError, match='source document paths'):
        entry.audit_candidate_file(receipt, hashlib.sha256(receipt.read_bytes()).hexdigest(),
                                   kwargs['trade_date'], ['000001.SZ'], kwargs['received_at'],
                                   provider='sina', request_url=kwargs['request_url'], source_document_paths={})
    monkeypatch.setattr(sys, 'argv', ['audit', '--candidate-provider', 'sina'])
    with pytest.raises(SystemExit):
        entry.main()


def _flow_selection_fixture():
    """Synthetic reviewed observations do not authorize any real vendor or request."""
    stages = ('product_access', 'original_source', 'required_security_scope',
              'complete_session', 'definition_alignment', 'original_receipt')
    candidate = {stage: dict(status='reviewed', evidence_sha256='a'*64,
        valid_from='2026-09-29', valid_through='2026-09-29', reason='synthetic review') for stage in stages}
    candidate['product_id'] = 'synthetic_other_product'
    candidate['original_source']['origin'] = 'synthetic_compute_vendor'
    candidate['required_security_scope']['required_scope_sha256'] = 'c'*64
    specifications = dict(order_grouping='original_order', trade_side='aggressor',
        size_buckets='fixture_same_buckets', session='fixture_complete_session',
        security_scope='fixture_same_scope', net_formula='fixture_main_buy-minus-sell')
    candidate['definition_alignment']['comparison_evidence'] = dict(valid_from='2026-09-29',
        valid_through='2026-09-29', source_specification_sha256=['a'*64, 'b'*64],
        canonical_definition='fixture_definition', specifications=[specifications.copy(), specifications.copy()])
    return candidate


def test_flow_product_selection_stops_at_missing_rights_source_scope_and_definition():
    from copy import deepcopy
    from trade_system.flow_contract import select_flow_product_candidate, VERIFIED_FLOW_COMPARISONS
    kwargs = dict(trade_date='2026-09-29', primary_origin='eastmoney', required_scope_sha256='c'*64)
    candidate = _flow_selection_fixture()
    ready = select_flow_product_candidate(candidate, **kwargs)
    assert ready['screening_complete'] and ready['next_action'] == 'existing_comparison_contract_review'
    assert not ready['independent_comparison_eligible'] and not ready['requests_authorized']
    assert not VERIFIED_FLOW_COMPARISONS and not ready['original_certification_gate_changed']
    assert ready['new_market_requests'] == ready['production_writes'] == 0
    missing = select_flow_product_candidate({'product_id': 'unknown_product'}, **kwargs)
    assert len(missing['blocking_reasons']) == 6 and missing['first_blocker'].startswith('product_access:')
    assert missing['next_action'] == 'stop_and_resolve_first_missing_precondition'
    unknown_scope = select_flow_product_candidate(candidate, '2026-09-29', primary_origin='eastmoney')
    assert not unknown_scope['required_scope_known'] and not unknown_scope['screening_complete']
    assert unknown_scope['first_blocker'] == 'required_security_scope:required_scope_unknown'
    for stage in ('product_access', 'original_source', 'required_security_scope',
                  'complete_session', 'definition_alignment'):
        changed = deepcopy(candidate)
        changed[stage]['status'] = 'unknown'
        result = select_flow_product_candidate(changed, **kwargs)
        assert not result['screening_complete'] and result['first_blocker'].startswith(stage + ':')
        assert result['next_action'] == 'stop_and_resolve_first_missing_precondition'
    same_source = deepcopy(candidate)
    same_source['original_source']['origin'] = ' EastMoney '
    assert 'same_or_unverified_original_compute_source' in select_flow_product_candidate(same_source, **kwargs)['first_blocker']
    shrunk = deepcopy(candidate)
    shrunk['required_security_scope']['required_scope_sha256'] = 'd'*64
    assert 'fingerprint_differs' in select_flow_product_candidate(shrunk, **kwargs)['first_blocker']
    mismatch = deepcopy(candidate)
    mismatch['definition_alignment']['comparison_evidence']['specifications'][1]['order_grouping'] = 'trade_print'
    assert 'order_grouping' in select_flow_product_candidate(mismatch, **kwargs)['first_blocker']
    no_receipt = deepcopy(candidate)
    no_receipt['original_receipt']['status'] = 'missing'
    observed = select_flow_product_candidate(no_receipt, **kwargs)
    assert observed['next_action'] == 'separate_bounded_request_budget_review' and not observed['requests_authorized']
    stale = deepcopy(candidate)
    stale['product_access']['valid_through'] = '2026-09-28'
    assert 'missing_hash_date' in select_flow_product_candidate(stale, **kwargs)['first_blocker']


def test_product_selection_cli_is_hash_bound_offline_and_cannot_escape_or_overwrite(tmp_path, monkeypatch):
    import hashlib
    import json
    import sys
    import pytest
    import trade_system.http_transport as transport
    from scripts import audit_stock_flow_contract as entry
    source = tmp_path/'reviewed-selection.json'
    raw = json.dumps({'product_id': 'existing_but_unqualified_product'}).encode()
    source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(entry, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(entry.duckdb, 'connect', lambda *a, **k: pytest.fail('selection opened DB'))
    monkeypatch.setattr(transport, 'read_verified_once', lambda *a, **k: pytest.fail('selection sent request'))
    args = ['audit', '--candidate-selection', str(source), '--selection-sha256', digest,
            '--date', '2026-09-29', '--primary-origin', 'eastmoney', '--required-scope-sha256', 'c'*64,
            '--db', 'production-must-not-open.duckdb']
    output = tmp_path/'reports'/'selection.json'
    monkeypatch.setattr(sys, 'argv', args + ['--out', str(output)])
    assert entry.main() == 0
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['selection_evidence_sha256'] == digest and len(result['blocking_reasons']) == 6
    assert not result['independent_comparison_eligible'] and result['new_market_requests'] == result['production_writes'] == 0
    original = output.read_bytes()
    with pytest.raises(SystemExit):
        entry.main()
    assert output.read_bytes() == original
    monkeypatch.setattr(sys, 'argv', args + ['--out', str(tmp_path/'escape.json')])
    with pytest.raises(SystemExit):
        entry.main()
    assert not (tmp_path/'escape.json').exists()
    source.write_bytes(raw + b' ')
    with pytest.raises(ValueError, match='SHA256 differs'):
        entry.audit_product_selection_file(source, digest, '2026-09-29', 'eastmoney', 'c'*64)
    duplicate = b'{"product_id":"first","product_id":"second"}'
    source.write_bytes(duplicate)
    with pytest.raises(ValueError, match='duplicate'):
        entry.audit_product_selection_file(source, hashlib.sha256(duplicate).hexdigest(), '2026-09-29', 'eastmoney', 'c'*64)
