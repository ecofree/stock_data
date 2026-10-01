import duckdb
from datetime import datetime

from trade_system.capital_flow_health import assess_capital_flow_health


def test_explicit_ttl_is_not_disabled_by_selecting_old_trade_date(tmp_path):
    db = tmp_path/'ttl.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP)')
        con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-09-11','000001',100,'2026-09-11 15:00:00')")
    expired = assess_capital_flow_health(db,'2026-09-11',max_age_seconds=7200,now=datetime(2026,9,12,10))
    assert expired['effective_max_age_seconds'] == 7200
    assert expired['stock_flow']['observed_codes'] == 0
    historical = assess_capital_flow_health(db,'2026-09-11',max_age_seconds=7200,now=datetime(2026,9,11,16))
    assert historical['stock_flow']['observed_codes'] == 1


def test_stale_and_nonfinite_rows_do_not_count_as_usable_flow():
    from trade_system.capital_flow_health import _relation_health
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP, is_stale BOOLEAN)')
        con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-09-11','A',100,'2026-09-11 15:00:00',true), ('2026-09-11','B','NaN','2026-09-11 15:00:00',false)")
        result = _relation_health(con,'multi_source_stock_flow','2026-09-11','stock_code',now=datetime(2026,9,11,16))
        assert result['codes'] == 0


def test_capital_flow_historical_as_of_rejects_future_writes(tmp_path):
    db_path = tmp_path / "future-flow.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow("
        "source_date DATE,stock_code VARCHAR,main_net DOUBLE,fetched_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE multi_source_sector_flow("
        "source_date DATE,sector_code VARCHAR,main_net DOUBLE,fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "('2026-07-31','000001',100,'2026-08-01 09:00:00')"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES "
        "('2026-07-31','BK1',100,'2026-08-01 09:00:00')"
    )
    con.close()

    result = assess_capital_flow_health(
        db_path,
        "2026-07-31",
        1,
        1,
        max_age_seconds=7200,
        now=datetime.fromisoformat("2026-07-31T17:45:00"),
    )

    assert result["ready"] is False
    assert result["stock_flow"]["observed_codes"] == 0
    assert result["sector_flow"]["observed_codes"] == 0


def test_capital_flow_aware_as_of_is_comparable_to_naive_db_time(tmp_path):
    db_path = tmp_path / "aware.duckdb"
    con = duckdb.connect(str(db_path))
    from test_readiness import _qualified_stock
    _qualified_stock(con, "2026-07-31")
    con.execute("UPDATE multi_source_stock_flow SET fetched_at='2026-07-31 17:45:00'")
    con.close()
    result = assess_capital_flow_health(
        db_path,
        "2026-07-31",
        expected_stock_codes=1,
        min_coverage_pct=99.5,
        max_age_seconds=1200,
        now=datetime.fromisoformat("2026-07-31T18:00:00+08:00"),
    )
    assert result["stock_flow"]["ready"] is True


def test_capital_flow_health_requires_real_sector_capital(tmp_path):
    db_path = tmp_path / "capital.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE l2_stock_intraday("
        "date DATE, stock_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_stock_intraday VALUES "
        "('2026-07-09','000001','2026-07-09 10:00:00')"
    )
    con.execute(
        "CREATE TABLE l2_sector_intraday("
        "date DATE, sector_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_sector_intraday VALUES "
        "('2026-07-09','801001','2026-07-09 10:00:00')"
    )
    con.close()

    result = assess_capital_flow_health(db_path, "2026-07-09", 1, 1)

    assert result["stock_flow"]["ready"] is False  # L2 curves are not qualified full-market funds
    assert result["sector_flow"]["ready"] is False
    assert result["ready"] is False

    current_run = assess_capital_flow_health(
        db_path,
        "2026-07-09",
        1,
        1,
        collected_after="2026-07-09T10:01:00",
    )
    assert current_run["stock_flow"]["ready"] is False
    stock = next(
        item
        for item in current_run["stock_flow"]["relations"]
        if item["relation"] == "l2_stock_intraday"
    )
    assert stock["status"] == "old_for_date"
    assert stock["recent_rows"] == 0


def test_capital_flow_health_enforces_expected_coverage(tmp_path):
    db_path = tmp_path / "coverage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE l2_stock_intraday(date DATE, stock_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_stock_intraday VALUES "
        "('2026-07-09','000001','2026-07-09 10:00:00'),"
        "('2026-07-09','000002','2026-07-09 10:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_capital(date DATE, sector_code VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_capital VALUES "
        "('2026-07-09','801001','2026-07-09 10:00:00')"
    )
    con.close()

    result = assess_capital_flow_health(db_path, "2026-07-09", 2, 2)

    assert result["stock_flow"]["ready"] is False  # L2 curves are not qualified full-market funds
    assert result["sector_flow"]["coverage_pct"] == 50.0
    assert result["sector_flow"]["ready"] is False
    assert result["ready"] is False


def test_capital_flow_health_accepts_fresh_migrated_flow_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "migrated-flow.duckdb"
    con = duckdb.connect(str(db_path))
    from test_readiness import _qualified_stock, _qualified_sector
    _qualified_stock(con, "2026-07-14", ("000001", "000002"))
    _qualified_sector(con, "2026-07-14")
    con.close()

    result = assess_capital_flow_health(
        db_path, "2026-07-14", 2, 2, collected_after="2026-07-14T09:00:00"
    )

    assert result["stock_flow"]["ready"] is True
    assert result["sector_flow"]["ready"] is True
    assert result["ready"] is True
    assert result['stock_flow']['qualification']['passed']
    assert result['sector_flow']['qualification']['passed']

    from scripts import check_capital_flow_health as entry
    import sys
    monkeypatch.setattr(entry,'assess_capital_flow_health',lambda *a,**k:result)
    args=['check_capital_flow_health.py','--date','2026-07-14','--out',str(tmp_path/'health.md')]
    monkeypatch.setattr(sys,'argv',args+['--stage','intraday'])
    assert entry.main()==0
    monkeypatch.setattr(sys,'argv',args+['--stage','close'])
    assert entry.main()==2  # Coverage is not after-close reconciliation.
    result['data_certified_ready']=False
    monkeypatch.setattr(sys,'argv',args+['--stage','intraday'])
    assert entry.main()==2


def test_capital_flow_health_rejects_count_complete_partial_sector_batch(tmp_path):
    db_path = tmp_path / "complete-partial-sector.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES ('2026-07-16','000001',100,'2026-07-16 10:00:00')"
    )
    con.execute(
        "CREATE TABLE multi_source_sector_flow(source_date DATE, sector_code VARCHAR, main_net DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES ('2026-07-16','BK0001',100,'2026-07-16 10:00:00')"
    )
    con.execute(
        "CREATE TABLE intraday_stock_flow_batch(trade_date DATE, expected_rows INTEGER)"
    )
    con.execute("INSERT INTO intraday_stock_flow_batch VALUES ('2026-07-16', 1)")
    con.execute(
        "CREATE TABLE intraday_sector_flow_batch(trade_date DATE, expected_rows INTEGER, fetched_rows INTEGER, coverage_pct DOUBLE, status VARCHAR)"
    )
    con.execute(
        "INSERT INTO intraday_sector_flow_batch VALUES ('2026-07-16', 1, 1, 100, 'partial')"
    )
    con.close()

    result = assess_capital_flow_health(db_path, "2026-07-16")

    assert result["sector_flow"]["ready"] is False
    assert result["ready"] is False


def test_damaged_independent_pass_cannot_survive_evidence_read_error(tmp_path, monkeypatch):
    from trade_system.schema import init_schema
    import trade_system.flow_contract as contract
    db_path = tmp_path/'damaged-reconciliation.duckdb'
    with duckdb.connect(str(db_path)) as con:
        init_schema(con)
        con.execute('''CREATE TABLE intraday_stock_flow_independent_reconciliation (
            trade_date DATE,status VARCHAR,overlap_reference_pct DOUBLE,correlation_main_net DOUBLE,
            sign_agreement_pct DOUBLE,primary_provider VARCHAR,reference_provider VARCHAR,
            evidence_json VARCHAR,rule_version VARCHAR)''')
        con.execute("INSERT INTO intraday_stock_flow_independent_reconciliation VALUES "
            "('2026-07-09','pass',100,1,100,'primary','tushare','{broken',"
            "'independent-flow-v2-amount-and-dated-scope')")
    monkeypatch.setattr(contract,'independent_comparison_contract',lambda *args:{'eligible':True})
    result = assess_capital_flow_health(db_path,'2026-07-09',1,1)
    assert result['reconciliation']['independent_status'] == 'unverified_or_changed_amount_evidence'
    assert result['reconciliation']['independent_reconciliation_ready'] is False


def test_health_and_cli_share_partial_unit_and_taxonomy_gate(tmp_path, monkeypatch):
    from test_readiness import _qualified_stock, _qualified_sector
    from scripts import check_capital_flow_health as entry
    import sys
    db = tmp_path/'shared-gate.duckdb'
    with duckdb.connect(str(db)) as con:
        _qualified_stock(con, '2026-09-29')
        _qualified_sector(con, '2026-09-29')
        con.execute("UPDATE intraday_stock_flow_batch SET status='partial'")
    result = assess_capital_flow_health(db, '2026-09-29', now=datetime(2026,9,29,10,1))
    assert not result['source_ready'] and not result['data_certified_ready']
    assert not result['stock_flow']['qualification']['passed']
    monkeypatch.setattr(entry, 'assess_capital_flow_health', lambda *a,**k:result)
    monkeypatch.setattr(sys, 'argv', ['check','--date','2026-09-29','--stage','intraday','--out',str(tmp_path/'health.md')])
    assert entry.main() == 2
    with duckdb.connect(str(db)) as con:
        con.execute("UPDATE intraday_stock_flow_batch SET status='success'")
        con.execute("UPDATE multi_source_stock_flow SET amount_unit='unknown'")
        con.execute("UPDATE intraday_sector_flow_taxonomy SET expected_rows=0 WHERE taxonomy='ths_concept'")
    result = assess_capital_flow_health(db, '2026-09-29', now=datetime(2026,9,29,10,1))
    assert not result['stock_flow']['ready'] and not result['sector_flow']['ready']
    assert not result['source_ready'] and not result['flow_certified_ready']


def test_reviewed_non_tushare_reference_is_consumed_without_brand_gate(tmp_path, monkeypatch):
    import json
    import trade_system.flow_contract as contract
    from test_readiness import _qualified_stock, _qualified_sector
    from scripts.reconcile_independent_stock_flow import reconcile
    day = '2026-09-29'
    db = tmp_path/'dynamic-reference.duckdb'
    with duckdb.connect(str(db)) as con:
        _qualified_stock(con, day, ('000001','000002'))
        _qualified_sector(con, day)
        con.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN is_stale BOOLEAN DEFAULT false")
        con.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN raw_json VARCHAR DEFAULT '{}'")
        con.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN collected_at TIMESTAMP")
        con.execute("UPDATE multi_source_stock_flow SET fetched_at='2026-09-29 17:00:00'")
        con.execute("INSERT INTO multi_source_stock_flow SELECT source_date,stock_code,main_net,fetched_at,"
                    "'ifind_fixture',amount_unit,'hithink_fixture','licensed_l2_fixture','reviewed_main_fixture',"
                    "'reviewed_adapter_fixture',false,raw_json,collected_at FROM multi_source_stock_flow")
        con.execute("UPDATE multi_source_sector_flow SET fetched_at='2026-09-29 17:00:00'")
        raw = json.loads(con.execute("SELECT raw_json FROM multi_source_sector_flow WHERE sector_type='ths_concept_derived'").fetchone()[0])
        raw['raw'].update(input_received_min=day+' 17:00:00',input_received_max=day+' 17:00:00')
        con.execute("UPDATE multi_source_sector_flow SET raw_json=? WHERE sector_type='ths_concept_derived'", [json.dumps(raw)])
        con.execute("CREATE TABLE intraday_stock_flow_reconciliation(trade_date DATE,status VARCHAR,reference_rows INTEGER,value_status VARCHAR)")
        con.execute("INSERT INTO intraday_stock_flow_reconciliation VALUES (?,'pass',2,'pass')", [day])
    axes = {k:'explicit_fixture_same_definition' for k in contract.FLOW_DEFINITION_AXES}
    signatures = tuple(sorted([('eastmoney','moneyflow_dc','provider_main_orders_net',contract.FLOW_MAPPING_VERSION),
                              ('hithink_fixture','licensed_l2_fixture','reviewed_main_fixture','reviewed_adapter_fixture')]))
    proof = dict(canonical_definition='fixture_only',valid_from=day,valid_through=day,
                 source_specification_sha256=['a'*64,'b'*64],specifications=[axes,axes],
                 amount_precision=dict(primary_quantum_yuan=1,reference_quantum_yuan=1))
    monkeypatch.setitem(contract.VERIFIED_FLOW_COMPARISONS, signatures, proof)
    assert reconcile(db,day,reference_provider='ifind_fixture')['status'] == 'pass'
    result = assess_capital_flow_health(db,day,now=datetime(2026,9,29,18),session_close=True)
    assert result['reconciliation']['independent_reference_provider'] == 'ifind_fixture'
    assert result['reconciliation']['independent_source_present']
    assert result['reconciliation']['independent_reconciliation_ready']
    assert result['flow_certified_ready']
    with duckdb.connect(str(db)) as con:
        con.execute("UPDATE multi_source_stock_flow SET main_net=main_net+1000 WHERE provider='ifind_fixture'")
    changed = assess_capital_flow_health(db,day,now=datetime(2026,9,29,18),session_close=True)
    assert not changed['flow_certified_ready']
    assert changed['reconciliation']['independent_status'] == 'unverified_or_changed_amount_evidence'
