import duckdb
import pytest
from datetime import datetime

import scripts.collect_intraday_sector_flow_full as collector
from trade_system.multi_source_store import MultiSourceStore


def _ths_fixture(store):
    """Frozen qualified-view fixture; does not claim online catalogue proof."""
    store.con.execute("CREATE TABLE fixture_catalog(trade_date DATE,concept_code VARCHAR,concept_name VARCHAR,stock_count INTEGER)")
    store.con.execute("INSERT INTO fixture_catalog VALUES ('2026-09-11','THS-A','A',2)")
    store.con.execute("CREATE TABLE fixture_members(trade_date DATE,concept_code VARCHAR,stock_code VARCHAR)")
    store.con.execute("INSERT INTO fixture_members VALUES ('2026-09-11','THS-A','000001'),('2026-09-11','THS-A','000002')")
    store.con.execute("CREATE VIEW v_default_concept_daily AS SELECT * FROM fixture_catalog")
    store.con.execute("CREATE VIEW v_default_concept_stock_history AS SELECT * FROM fixture_members")
    for code, value in [('000001',10),('000002',20)]:
        store.store('stock_flow', code, [{'date':'2026-09-11','main_net':value,'super_net':value,
                    'amount_unit':'yuan','flow_definition':'provider_main_net'}],
                    {'source':'eastmoney_market','status':'live'})
    store.con.execute("UPDATE multi_source_stock_flow SET fetched_at='2026-09-11 16:00:00'")
    store.store('sector_flow', None, [dict(_row('OLD'),sector_type='ths_concept_derived')],
                {'source':'derived_ths_stock_aggregate','status':'live'},trade_date='2026-09-11')


def test_ths_complete_aggregate_replaces_slice_and_preserves_unknown_buckets(tmp_path):
    with MultiSourceStore(tmp_path/'ths.duckdb') as store:
        _ths_fixture(store)
        result = collector._publish_ths_aggregate(store,'2026-09-11',now=datetime(2026,9,11,17))
        assert result['promoted'] and result['status']=='success'
        assert store.con.execute('SELECT sector_code,main_net,large_net FROM multi_source_sector_flow').fetchall() == [('THS-A',30,None)]
        assert result['membership_pairs']==2 and result['missing_stock_count']==0


@pytest.mark.parametrize('fault,status',[
    ("DELETE FROM multi_source_stock_flow WHERE stock_code='000002'",'partial_stock_flow'),
    ("UPDATE multi_source_stock_flow SET main_net='NaN' WHERE stock_code='000002'",'partial_stock_flow'),
    ("UPDATE multi_source_stock_flow SET is_stale=TRUE WHERE stock_code='000002'",'partial_stock_flow'),
    ("UPDATE multi_source_stock_flow SET amount_unit='unknown' WHERE stock_code='000002'",'partial_stock_flow'),
    ("UPDATE multi_source_stock_flow SET flow_definition='total_net_only' WHERE stock_code='000002'",'partial_stock_flow'),
    ("UPDATE multi_source_stock_flow SET fetched_at='2026-09-11 18:00:00' WHERE stock_code='000002'",'partial_stock_flow'),
    ("UPDATE multi_source_stock_flow SET fetched_at='2026-09-11 10:00:00' WHERE stock_code='000002'",'partial_stock_flow'),
    ("DELETE FROM fixture_members WHERE stock_code='000002'",'partial_members'),
    ("INSERT INTO fixture_members SELECT * FROM fixture_members LIMIT 1",'partial_members'),
    ("UPDATE multi_source_stock_flow SET main_net=1e308",'invalid_aggregate'),
])
def test_ths_bad_inputs_never_overwrite_good_snapshot(tmp_path, fault, status):
    with MultiSourceStore(tmp_path/'ths-fault.duckdb') as store:
        _ths_fixture(store)
        before = store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()
        store.con.execute(fault)
        result = collector._publish_ths_aggregate(store,'2026-09-11',now=datetime(2026,9,11,17))
        assert result['status']==status and not result['promoted']
        assert store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()==before
        assert store.con.execute("SELECT count(*) FROM multi_source_observation WHERE data_type='ths_aggregate_batch'").fetchone()[0]==1


def test_ths_stale_members_and_write_failure_preserve_snapshot(tmp_path, monkeypatch):
    with MultiSourceStore(tmp_path/'ths-rollback.duckdb') as store:
        _ths_fixture(store)
        before = store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()
        stale = collector._publish_ths_aggregate(store,'2026-09-22',now=datetime(2026,9,22,17))
        assert stale['status']=='stale_members' and not stale['promoted']
        original = store.store
        def broken(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[0]=='sector_flow':
                result['rows_written']=0
            return result
        monkeypatch.setattr(store,'store',broken)
        failed = collector._publish_ths_aggregate(store,'2026-09-11',now=datetime(2026,9,11,17))
        assert failed['status']=='error' and not failed['promoted']
        assert store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()==before


def test_sector_legacy_projection_rejects_namespace_collision(tmp_path):
    with MultiSourceStore(tmp_path/'collision.duckdb') as store:
        store.con.execute('CREATE TABLE sector_capital(date DATE,sector_code VARCHAR,main_net_inflow DOUBLE,super_net_inflow DOUBLE,big_net_inflow DOUBLE,mid_net_inflow DOUBLE,small_net_inflow DOUBLE)')
        store.con.execute("INSERT INTO sector_capital VALUES ('2026-09-11','OLD',7,0,0,0,0)")
        for provider, kind in [('eastmoney_sector_full','em_industry'),('derived_ths_stock_aggregate','ths_concept_derived')]:
            store.store('sector_flow',None,[dict(_row('SAME'),sector_type=kind)],
                        {'source':provider,'status':'live'},trade_date='2026-09-11')
        with pytest.raises(ValueError,match='namespace/measure collision'):
            store.sync_sector_capital('2026-09-11')
        assert store.con.execute('SELECT sector_code,main_net_inflow FROM sector_capital').fetchall()==[('OLD',7)]


def test_sector_same_source_identity_cannot_silently_change_taxonomy(tmp_path):
    with MultiSourceStore(tmp_path/'type-change.duckdb') as store:
        store.store('sector_flow',None,[_row('A')],{'source':'eastmoney_sector_full','status':'live'},trade_date='2026-09-11')
        before = store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()
        with pytest.raises(ValueError,match='changed taxonomy'):
            store.store('sector_flow',None,[dict(_row('A'),sector_type='ths_concept_derived')],
                        {'source':'eastmoney_sector_full','status':'live'},trade_date='2026-09-11')
        assert store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()==before


def test_collector_and_recovery_share_ths_publication_guard(tmp_path, monkeypatch):
    db = tmp_path/'shared-guard.duckdb'
    with MultiSourceStore(db) as store:
        _ths_fixture(store)
    # Frozen view fixture bypasses schema regeneration, not the publication
    # guard. The actual two entrypoints and actual transactional store run.
    monkeypatch.setattr(collector, 'init_schema', lambda con: None)
    monkeypatch.setattr(collector, '_from_em_sector_flow_page', lambda **k: ([_row('EM')],{'total':1}))
    monkeypatch.setattr(collector.shared_host_limiter, 'acquire', lambda *a: None)
    original = collector._publish_ths_aggregate
    calls = []
    def frozen_clock(store, day, **kwargs):
        calls.append(day)
        return original(store,day,now=datetime(2026,9,11,17))
    monkeypatch.setattr(collector, '_publish_ths_aggregate', frozen_clock)
    result = collector.collect_full_sector_flow(db,'2026-09-11',expected_codes=['EM'],
        catalogue_version='fixture',max_pages=1,pause_seconds=0)
    assert result['ths_aggregation']['promoted']
    with MultiSourceStore(db) as store:
        before=store.con.execute("SELECT * FROM multi_source_sector_flow WHERE provider='derived_ths_stock_aggregate'").fetchall()
        store.con.execute("DELETE FROM multi_source_stock_flow WHERE stock_code='000002'")
    recovered = collector.rebuild_ths_derived_flow(db,'2026-09-11')
    assert recovered['status']=='partial_stock_flow' and not recovered['promoted']
    with MultiSourceStore(db) as store:
        assert store.con.execute("SELECT * FROM multi_source_sector_flow WHERE provider='derived_ths_stock_aggregate'").fetchall()==before
        assert store.con.execute("SELECT status FROM intraday_sector_flow_taxonomy WHERE taxonomy='ths_concept'").fetchone()[0]=='partial_stock_flow'
    assert calls==['2026-09-11','2026-09-11']


def test_review_consumes_only_complete_subset_and_never_old_extreme_rank(tmp_path):
    from trade_system.review_queries import _apply_qualified_concept_flow
    from trade_system.concept_flow import _prepare_ths_aggregate
    from datetime import timezone
    with MultiSourceStore(tmp_path/'subset.duckdb') as store:
        _ths_fixture(store)
        store.con.execute("INSERT INTO fixture_catalog VALUES ('2026-09-11','THS-B','B',1),('2026-09-11','THS-C','C',1)")
        store.con.execute("INSERT INTO fixture_members VALUES ('2026-09-11','THS-B','000001'),('2026-09-11','THS-C','000003')")
        store.store('stock_flow','000003',[{'date':'2026-09-11','main_net':-8,'amount_unit':'yuan','flow_definition':'provider_main_net'}],
            {'source':'eastmoney_market','status':'live'})
        store.con.execute("UPDATE multi_source_stock_flow SET fetched_at='2026-09-11 16:00:00'")
        store.con.execute("DELETE FROM multi_source_stock_flow WHERE stock_code='000002'")
        before = store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()
        old = {'sector_inflow':[{'sector_code':'OLD','main_net':1e100}], 'sector_flow_persistence':[{'sector_code':'OLD'}]}
        result = _apply_qualified_concept_flow(old,store.con,'2026-09-11',now=datetime(2026,9,11,9,tzinfo=timezone.utc))
        assert [r['sector_code'] for r in result['sector_inflow']]==['THS-B']
        assert [r['sector_code'] for r in result['sector_outflow']]==['THS-C']
        excluded=result['qualified_concept_flow']['contract']['excluded_concepts']
        assert excluded[0]['sector_code']=='THS-A' and excluded[0]['missing_members']==['000002']
        assert result['sector_flow_persistence']==[]
        assert '2/3' in result['coverage_alerts'][0]
        assert store.con.execute('SELECT * FROM multi_source_sector_flow').fetchall()==before
        strict, contract = _prepare_ths_aggregate(store.con,'2026-09-11',now=datetime(2026,9,11,17))
        assert strict==[] and contract['status']=='partial_stock_flow'
        expired=_apply_qualified_concept_flow(result,store.con,'2026-09-11',now=datetime(2026,9,11,21))
        assert expired['sector_inflow']==expired['sector_outflow']==[]
        assert len(expired['qualified_concept_flow']['contract']['excluded_concepts'])==3


def test_subset_missing_catalogue_cannot_fall_back_to_retained_rank(tmp_path):
    from trade_system.review_queries import _apply_qualified_concept_flow
    with MultiSourceStore(tmp_path/'no-catalogue.duckdb') as store:
        result=_apply_qualified_concept_flow({'sector_inflow':[{'sector_code':'OLD'}]},store.con,'2026-09-11',now=datetime(2026,9,11,17))
        assert result['sector_inflow']==result['sector_outflow']==[]
        assert result['qualified_concept_flow']['contract']['full_snapshot_promoted'] is False


def _three_session_fixture(store):
    _ths_fixture(store)
    con=store.con
    con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)')
    for day in ['2026-09-09','2026-09-10','2026-09-11']:
        con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE',?,true)",[day])
        if day=='2026-09-11':continue
        con.execute("INSERT INTO fixture_catalog SELECT CAST(? AS DATE),concept_code,concept_name,stock_count FROM fixture_catalog WHERE trade_date='2026-09-11'",[day])
        con.execute("INSERT INTO fixture_members SELECT CAST(? AS DATE),concept_code,stock_code FROM fixture_members WHERE trade_date='2026-09-11'",[day])
        con.execute("INSERT INTO multi_source_stock_flow SELECT * REPLACE (CAST(? AS DATE) AS source_date,CAST(? AS TIMESTAMP) AS fetched_at) FROM multi_source_stock_flow WHERE source_date='2026-09-11'",[day,day+' 16:00:00'])


def test_fixed_member_history_and_candidate_context_do_not_change_scores(tmp_path):
    from trade_system.concept_flow import qualified_concept_review,matched_concept_history,explain_concept_candidates
    with MultiSourceStore(tmp_path/'history.duckdb') as store:
        _three_session_fixture(store)
        q=qualified_concept_review(store.con,'2026-09-11',now=datetime(2026,9,11,17))
        h=matched_concept_history(store.con,'2026-09-11',q,now=datetime(2026,9,11,17))
        assert h['rows'][0]['main_net']==90 and h['rows'][0]['flow_streak']==3
        candidates=[{'stock_code':'000001','score':17},{'stock_code':'999999','score':8}]
        e=explain_concept_candidates(candidates,q,h)
        assert [r['score'] for r in e]==[17,8]
        assert e[0]['concept_evidence'][0]['positive_days']==3
        assert e[1]['concept_evidence_status']=='no_qualified_concept_not_negative_signal'


@pytest.mark.parametrize('change',[
    "DELETE FROM multi_source_stock_flow WHERE source_date='2026-09-10' AND stock_code='000002'",
    "UPDATE fixture_members SET stock_code='000004' WHERE trade_date='2026-09-10' AND stock_code='000002'",
    "UPDATE multi_source_stock_flow SET provider='eastmoney_intraday_clist_delay' WHERE source_date='2026-09-10' AND stock_code='000002'",
])
def test_history_rejects_missing_day_member_or_source_change(tmp_path,change):
    from trade_system.concept_flow import qualified_concept_review,matched_concept_history
    with MultiSourceStore(tmp_path/'history-change.duckdb') as store:
        _three_session_fixture(store)
        store.con.execute(change)
        q=qualified_concept_review(store.con,'2026-09-11',now=datetime(2026,9,11,17))
        h=matched_concept_history(store.con,'2026-09-11',q,now=datetime(2026,9,11,17))
        assert h['rows'][0]['history_status']!='matched'
        assert h['rows'][0]['main_net'] is None


def _row(code: str) -> dict:
    return {
        "sector_code": code,
        "sector_name": f"board-{code}",
        "main_net": 100.0,
        "super_net": 40.0,
        "large_net": 60.0,
        "mid_net": 0.0,
        "small_net": 0.0,
        "sector_type": "em_industry",
        "amount_unit": "yuan",
    }


def test_taxonomy_success_threshold_is_uniformly_99_point_5_percent():
    assert collector._taxonomy_coverage_status(200, 199) == (99.5, "success")
    assert collector._taxonomy_coverage_status(201, 200) == (99.5, "success")
    assert collector._taxonomy_coverage_status(496, 486) == (97.98, "partial")
    assert collector._taxonomy_coverage_status(
        496, 496, unverified=True
    ) == (100.0, "unverified")


def test_reverse_code_reconciliation_closes_dynamic_page_gap(tmp_path, monkeypatch):
    db = tmp_path / "sector-pages.duckdb"
    calls = []

    def fake_page(*, page, page_size, return_meta, sort_field, sort_order):
        calls.append((page, sort_field, sort_order))
        if sort_order == "0":
            rows = {
                1: [_row("BK0001"), _row("BK0002")],
                # Simulate a board moving across a page boundary while the
                # first sweep is running.
                2: [_row("BK0002"), _row("BK0003")],
            }.get(page, [])
        else:
            rows = {
                1: [_row("BK0004"), _row("BK0003")],
                2: [_row("BK0002"), _row("BK0001")],
            }.get(page, [])
        return rows, {"total": 4, "returned_rows": len(rows)}

    monkeypatch.setattr(collector, "_from_em_sector_flow_page", fake_page)
    monkeypatch.setattr(collector.shared_host_limiter, "acquire", lambda *args, **kwargs: None)

    result = collector.collect_full_sector_flow(
        db,
        "2026-07-24",
        page_size=500,
        max_pages=2,
        pause_seconds=0,
        expected_codes=['BK0001','BK0002','BK0003','BK0004'], catalogue_version='fixture-v1',
    )

    assert result["status"] == "success_with_optional_gap"
    assert result["coverage_pct"] == 100.0
    assert result["reconciliation_pages"] == 1
    assert calls == [
        (1, "f12", "0"),
        (2, "f12", "0"),
        (1, "f12", "1"),
    ]
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
            "WHERE source_date='2026-07-24' AND provider='eastmoney_sector_full'"
        ).fetchone()[0] == 4
        assert con.execute(
            "SELECT status FROM intraday_sector_flow_batch "
            "WHERE trade_date='2026-07-24'"
        ).fetchone()[0] == "success_with_optional_gap"
        assert con.execute(
            "SELECT count(*) FROM sector_capital WHERE date='2026-07-24'"
        ).fetchone()[0] == 4
    finally:
        con.close()


@pytest.mark.parametrize('failure', ['duplicate_pages', 'changed_total', 'wrong_date', 'nonfinite'])
def test_incomplete_refresh_preserves_last_snapshot(tmp_path, monkeypatch, failure):
    db = tmp_path/'preserved.duckdb'
    with MultiSourceStore(db) as store:
        store.store('sector_flow', None, [_row('OLD')],
                    {'source':'eastmoney_sector_full','status':'live'}, trade_date='2026-07-24')
    def page(**kwargs):
        row = _row('NEW')
        if failure == 'wrong_date': row['date'] = '2026-07-23'
        if failure == 'nonfinite':
            for field in ('main_net','super_net','large_net','mid_net','small_net'): row[field] = float('nan')
        total = 2 if failure != 'changed_total' or kwargs['page'] == 1 else 1
        return [row], {'total': total}
    monkeypatch.setattr(collector, '_from_em_sector_flow_page', page)
    monkeypatch.setattr(collector.shared_host_limiter, 'acquire', lambda *a: None)
    monkeypatch.setattr(collector.time, 'sleep', lambda *a: None)
    result = collector.collect_full_sector_flow(db, '2026-07-24', max_pages=2, pause_seconds=0)
    assert not result['status'].startswith('success')
    assert result['pagination']['promoted'] is False
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT sector_code FROM multi_source_sector_flow WHERE provider='eastmoney_sector_full'").fetchall() == [('OLD',)]
        assert con.execute("SELECT count(*) FROM multi_source_observation").fetchone()[0] > 1


def test_expected_membership_rejects_equal_count_wrong_identities(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, '_from_em_sector_flow_page', lambda **k: ([_row('A'), _row('X')], {'total':2}))
    monkeypatch.setattr(collector.shared_host_limiter, 'acquire', lambda *a: None)
    result = collector.collect_full_sector_flow(tmp_path/'membership.duckdb', '2026-07-24',
        max_pages=1, pause_seconds=0, expected_codes=['A','B'], catalogue_version='frozen-fixture-v1')
    assert result['pagination']['missing_codes'] == ['B']
    assert result['pagination']['unexpected_codes'] == ['X']
    assert not result['pagination']['promoted']


def test_failed_provider_status_cannot_overwrite_accepted_flow(tmp_path):
    with MultiSourceStore(tmp_path/'status.duckdb') as store:
        store.store('sector_flow', None, [_row('A')], {'source':'eastmoney','status':'live'}, trade_date='2026-07-24')
        failed = dict(_row('A'), main_net=-999)
        result = store.store('sector_flow', None, [failed], {'source':'eastmoney','status':'failed'}, trade_date='2026-07-24')
        assert result['rows_written'] == 0
        assert store.con.execute('SELECT main_net FROM multi_source_sector_flow').fetchone()[0] == 100


def test_count_only_capture_is_observation_not_full_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, '_from_em_sector_flow_page', lambda **k: ([_row('A')], {'total':1}))
    monkeypatch.setattr(collector.shared_host_limiter, 'acquire', lambda *a: None)
    result = collector.collect_full_sector_flow(tmp_path/'count-only.duckdb', '2026-07-24', max_pages=1, pause_seconds=0)
    assert result['pagination']['status'] == 'unverified_catalogue'
    assert not result['pagination']['promoted']


def test_atomic_publication_rollback_keeps_previous_rows(tmp_path, monkeypatch):
    with MultiSourceStore(tmp_path/'rollback.duckdb') as store:
        store.store('sector_flow', None, [_row('OLD')], {'source':'eastmoney_sector_full','status':'live'}, trade_date='2026-07-24')
        monkeypatch.setattr(store, 'store', lambda *a, **k: {'rows_written':0})
        pagination = {'status':'complete','expected_total':1,'catalogue_version':'fixture','promoted':False}
        with pytest.raises(ValueError, match='changed'):
            collector._replace_sector_snapshot(store,'2026-07-24',[_row('NEW')],pagination)
        assert store.con.execute('SELECT sector_code FROM multi_source_sector_flow').fetchall() == [('OLD',)]


def test_sector_contract_keeps_namespace_measure_and_raw_type(tmp_path):
    import json
    with MultiSourceStore(tmp_path/'taxonomy.duckdb') as store:
        row = dict(_row('SAME'), sector_type='ths_industry', amount_unit='100m_yuan', main_net=2)
        store.store('sector_flow', None, [row], {'source':'tushare','status':'live'}, trade_date='2026-07-24')
        stored = store.con.execute('SELECT main_net,amount_unit,raw_json FROM multi_source_sector_flow').fetchone()
        assert stored[:2] == (200000000, 'yuan')
        contract = json.loads(stored[2])['canonical_contract']
        assert contract['taxonomy_namespace'] == 'ths.industry'
        assert contract['flow_definition'] == 'sector_total_net'
        assert contract['raw_sector_type'] == 'ths_industry'
        assert json.loads(stored[2])['main_net'] == 2


def test_unknown_sector_type_never_becomes_em_from_code_prefix():
    from trade_system.flow_contract import normalize_sector_flow_row
    result = normalize_sector_flow_row(dict(_row('BK001'),sector_type='未认证类别'), 'tushare_sector_full')
    assert result['sector_type'] == 'unknown'
    assert result['taxonomy_namespace'] is None
    assert result['quality_reason'] == 'unsupported_taxonomy'
