"""Business counterexamples on isolated databases and immutable page bundles."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import shutil

import duckdb
import pytest

from trade_system import business_contract as contract
from trade_system.collection_profiles import PHASES, phase_tasks, product_usage
from trade_system.gate_contract import build_operator_state
from trade_system.snapshot_contract import historical_worklist, membership_contract


ROOT = Path(__file__).resolve().parents[1]


def test_catalogue_covers_every_retained_task_and_references_real_consumer_code():
    catalogue = contract.catalogue()
    for phase in PHASES:
        for task in phase_tasks(phase):
            edge = contract.task_contract(task.name)
            assert edge['products'] and edge['evidence_scope'] == 'declared_edges_not_observed_consumption'
    for p in catalogue['products'].values():
        assert p['consumers'] or p['retention_reason']
        for consumer in p['consumers']:
            module, name = consumer.rsplit('.', 1)
            tree = ast.parse((ROOT / (module.replace('.', '/') + '.py')).read_text(encoding='utf-8'))
            assert name in {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}, consumer
    with pytest.raises(KeyError):
        contract.task_contract('new_unregistered_collector')


def test_source_generated_documents_cannot_drift_from_current_contract():
    from trade_system.source_authority import render_policy_markdown, policy
    assert (ROOT/'docs/business_product_contract.md').read_text(encoding='utf-8') == contract.render_markdown()
    assert (ROOT/'docs/source_authority_matrix.md').read_text(encoding='utf-8') == render_policy_markdown()
    for key in ('qlib', 'operator_outcome'):
        assert policy(key).lifecycle == 'historical_read_only'
        assert policy(key).write_owner.startswith('none')
    assert contract.PRODUCTS['auction_match'].policy == 'auction_match'
    assert policy('auction_match').provider_order == ('xiaodefa',)
    assert policy('auction_match') != policy('auction')


def test_legacy_candidate_and_all_green_data_never_open_current_execution():
    from trade_system.readiness import render_readiness_markdown
    state = build_operator_state(source_ready=True, pipeline_ready=True, artifact_current=True,
        data_certified_ready=True, flow_certified_ready=True, execution_ready=True, run_status='not_published')
    assert state['analysis_ready'] and state['operator_status'] == 'analysis_only'
    assert state['legacy_execution_requested'] and not state['execution_ready']
    old = dict(state, execution_ready=True, executable_candidates=99, trade_date='2026-10-12',
               stage='close', missing_groups=[], groups=[])
    text = render_readiness_markdown(old)
    assert 'Execution ready: `false`' in text and 'Executable candidates: `0`' in text
    assert 'Execution ready: `true`' not in text


def test_old_execution_true_cannot_be_rendered_as_current_authority():
    from trade_system.review_web import _render_loop, _render_plans, _render_data_gates
    from trade_system.daily_review import render_daily_review_markdown
    old = {'readiness': {'operator_status': 'executable', 'analysis_ready': True,
                        'execution_ready': True, 'executable_candidates': 99},
           'execution_control': {'execution_ready': True, 'override': 'ALLOW'},
           'plans': [{'stock_code': '000001', 'status': 'planned'}]}
    for renderer in (_render_loop, _render_plans):
        page = renderer(copy.deepcopy(old))
        assert '研究计划草案' in page and '历史标记不授予执行权限' in page
        assert '今日执行计划' not in page and '已通过当前执行门禁' not in page
    page = _render_data_gates(old)
    assert 'analysis_only' in page and 'executable' not in page
    text = render_daily_review_markdown(old)
    assert 'Current execution ready: `false`; current executable candidates: `0`' in text
    assert 'Historical candidate flags' in text and '/ 99`' in text


def test_transport_attribution_preserves_safe_edges_without_unbounded_or_secret_fields(monkeypatch):
    from urllib.request import Request
    from trade_system.http_transport import request_receipt
    context = {'data_product_ids': ['daily_price'], 'declared_consumers': ['trade_system.v2.market_workspace.project'],
               'business_contract_sha256': contract.contract_hash(), 'secret': 'synthetic-secret'}
    monkeypatch.setenv('STOCKDATA_REQUEST_CONTEXT', json.dumps(context))
    with request_receipt(Request('https://example.invalid/never-sent')) as receipt:
        assert receipt['attribution'] == {k: v for k, v in context.items() if k != 'secret'}
    for bad in (['daily_price?token=secret'], ['daily_price'] * 33, ['daily_price', 7]):
        monkeypatch.setenv('STOCKDATA_REQUEST_CONTEXT', json.dumps({'data_product_ids': bad}))
        with request_receipt(Request('https://example.invalid/never-sent')) as receipt:
            assert 'data_product_ids' not in receipt['attribution']


@pytest.mark.parametrize('snapshot,status,usable,current', [
    ('2026-10-12', 'complete', True, True), ('2026-10-11', 'complete', True, False),
    ('2026-10-04', 'complete', False, False), ('2026-10-13', 'complete', False, False),
    ('2026-10-12', 'partial', False, False), (None, 'missing', False, False)])
def test_membership_clock_is_not_an_effective_interval(snapshot,status,usable,current):
    result = membership_contract('2026-10-12',snapshot,status,{'T':['000001']},max_age_days=7)
    assert result['usable_for_retained_review'] is usable
    assert result['same_session_membership'] is current
    assert result['effective_from'] is result['effective_through'] is None
    assert not result['absence_proves_exit'] and not result['point_in_time_qualified']


def test_membership_fingerprint_changes_with_identity_and_rejects_duplicates():
    a = membership_contract('2026-10-12','2026-10-12','complete',{'T':['000001']},max_age_days=7)
    b = membership_contract('2026-10-12','2026-10-12','complete',{'T':['000002']},max_age_days=7)
    assert a['member_keys_sha256'] != b['member_keys_sha256']
    with pytest.raises(ValueError,match='duplicate'):
        membership_contract('2026-10-12','2026-10-12','complete',{'T':['000001','000001']},max_age_days=7)
    assert not membership_contract('2026-10-12','2026-10-12','complete',{},max_age_days=7)['same_session_membership']


def market_fixture():
    from trade_system.v2.market_workspace import project
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,is_open INTEGER,cal_date DATE)')
        con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE',1,'2026-10-12'),('SZSE',1,'2026-10-12')")
        con.execute('CREATE TABLE v_kline_daily(trade_date VARCHAR,stock_code VARCHAR,change_pct DOUBLE,'
                    'provider VARCHAR,adjustment VARCHAR,volume_unit VARCHAR,amount_unit VARCHAR,fetched_at TIMESTAMP,close DOUBLE)')
        con.execute("INSERT INTO v_kline_daily VALUES ('2026-10-12','000001',2,'xiaodefa','none','shares','yuan','2026-10-12 16:00',10)")
        con.execute('CREATE TABLE v_default_concept_daily(trade_date DATE,concept_code VARCHAR,concept_name VARCHAR,stock_count INTEGER)')
        con.execute("INSERT INTO v_default_concept_daily VALUES ('2026-10-12','T','合成题材',2)")
        con.execute('CREATE TABLE v_default_concept_stock_history(trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,stock_name VARCHAR)')
        con.execute("INSERT INTO v_default_concept_stock_history VALUES ('2026-10-12','T','000001','合成甲'),('2026-10-12','T','000002','合成乙')")
        return project(con,'2026-10-12','2026-10-12T18:00:00+08:00',set())


def test_actual_projection_and_published_bundle_report_consumption_not_acquisition(tmp_path):
    from trade_system.v2.research_product import publish_desk
    market = market_fixture()
    assert market['membership_contract']['same_session_membership']
    assert market['stocks']['000002']['change_pct'] is None
    publish_desk(tmp_path,market=market)
    measured = contract.read_publication_consumption(tmp_path/'publication')
    assert measured['status'] == 'verified_inline_consumption'
    assert measured['current_contract'] and not measured['execution_ready']
    assert all(r['source_rows_published'] is None and r['source_request_link'] == 'unverified' for r in measured['receipts'])
    assert 'chips' not in {r['product_id'] for r in measured['receipts']}
    usage = product_usage(publication_roots=[tmp_path/'publication'])
    price = next(p for p in usage['products'] if p['product_id']=='sync_tushare_close')
    assert price['publication_consumption'] and price['observations'] == []
    assert usage['avoidable_cost'] is None  # No invented savings or exact request join.


def test_failed_or_forged_consumption_does_not_advance_publication(tmp_path):
    from trade_system.v2.research_product import publish_desk
    market = market_fixture()
    publish_desk(tmp_path,market=market)
    pointer = tmp_path/'publication/current.json'
    before = pointer.read_bytes()
    forged = copy.deepcopy(market)
    forged['product_consumption']['receipts'][0]['value_sha256'] = '0'*64
    with pytest.raises(ValueError,match='consumption'):
        publish_desk(tmp_path,market=forged)
    assert pointer.read_bytes() == before
    assert contract.read_publication_consumption(tmp_path/'publication')['status']=='verified_inline_consumption'


@pytest.mark.parametrize('invalid', [{}, None, False, []])
def test_present_but_empty_consumption_cannot_bypass_publication_validation(tmp_path,invalid):
    from trade_system.v2.research_product import publish_desk
    market=market_fixture();publish_desk(tmp_path,market=market)
    pointer=tmp_path/'publication/current.json';before=pointer.read_bytes()
    market['product_consumption']=invalid
    with pytest.raises(ValueError,match='consumption'):
        publish_desk(tmp_path,market=market)
    assert pointer.read_bytes()==before


@pytest.mark.parametrize('field,value',[('trade_date','2026-10-13'),('as_of','2026-10-12T19:00:00+08:00')])
def test_changed_projection_clock_cannot_reuse_old_consumption(tmp_path,field,value):
    from trade_system.v2.research_product import publish_desk
    market=market_fixture();market[field]=value
    with pytest.raises(ValueError,match='consumption'):
        publish_desk(tmp_path,market=market)
    assert not (tmp_path/'publication/current.json').exists()


def test_read_only_usage_mode_never_opens_database_or_collects(tmp_path,monkeypatch,capsys):
    import sys
    from scripts import run_integrated_daily as runner
    missing=tmp_path/'never-opened.duckdb'
    monkeypatch.setattr(sys,'argv',['run_integrated_daily','--db',str(missing),'--product-usage'])
    assert runner.main()==0
    result=json.loads(capsys.readouterr().out)
    assert result['products'] and not result['execution_ready'] and not missing.exists()
    for roots in ([],[str(tmp_path)]):
        monkeypatch.setattr(sys,'argv',['run_integrated_daily','--publication-usage',*roots])
        with pytest.raises(SystemExit) as exc:runner.main()
        assert exc.value.code==2


def test_historical_bundle_without_instrumentation_stays_unmeasured(tmp_path):
    from trade_system.v2.research_product import publish_desk
    from trade_system.v2.domain import identity
    market = market_fixture()
    market.pop('product_consumption'); market.pop('snapshot_id')
    market['snapshot_id'] = identity(market)
    publish_desk(tmp_path,market=market)
    assert contract.read_publication_consumption(tmp_path/'publication')['status']=='unmeasured_historical_bundle'


def test_published_bytes_modified_after_publication_are_rejected(tmp_path):
    from trade_system.v2.research_product import publish_desk
    market = market_fixture(); pointer=publish_desk(tmp_path,market=market)
    target=tmp_path/'publication/runs'/pointer['run_id']/'desk.json'
    target.write_bytes(target.read_bytes()+b' ')
    with pytest.raises(ValueError,match='checksum'):
        contract.read_publication_consumption(tmp_path/'publication')


def migration_folder(tmp_path):
    folder=tmp_path/'migrations';folder.mkdir()
    for name in ('0016_kline_product_identity.sql','0017_daily_count_provenance.sql'):
        shutil.copy2(ROOT/'migrations'/name,folder/name)
    return folder


def test_both_migrations_keep_products_values_unknowns_and_are_idempotent(tmp_path):
    from trade_system.migrations import apply_pending
    folder=migration_folder(tmp_path)
    with duckdb.connect(str(tmp_path/'isolated.duckdb')) as con:
        con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,'
                    'provider VARCHAR,adjustment VARCHAR,close DOUBLE)')
        con.execute("INSERT INTO multi_source_kline VALUES ('2026-10-12','stock','000001','a','none',10),"
                    "('2026-10-12','stock','000001','a','qfq',5),('2026-10-12','stock','000001','b','none',11),"
                    "('2026-10-12','stock','000002','a',NULL,12)")
        con.execute('CREATE TABLE daily_summary(date DATE,limit_up_count INTEGER)')
        con.execute("INSERT INTO daily_summary VALUES ('2026-10-12',NULL)")
        before=con.execute('SELECT * FROM multi_source_kline ORDER BY ALL').fetchall()
        assert apply_pending(con,folder)==[16,17]
        assert apply_pending(con,folder)==[]
        assert con.execute('SELECT * FROM multi_source_kline ORDER BY ALL').fetchall()==before
        assert con.execute('SELECT count_provenance_json FROM daily_summary').fetchone()==(None,)
        work=historical_worklist(con)
        rows={r['id']:r for r in work['items']}
        assert rows['unknown_price_identity']['count']==1
        assert rows['market_count_lineage']['count']==1
        assert rows['sector_stocks_duplicates']['count'] is None
        assert not work['repairs_applied']


def test_migration_conflict_preserves_data_and_no_partial_version_or_next_migration(tmp_path):
    from trade_system.migrations import apply_pending
    folder=migration_folder(tmp_path)
    with duckdb.connect(str(tmp_path/'conflict.duckdb')) as con:
        con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,provider VARCHAR,adjustment VARCHAR,close DOUBLE)')
        con.execute("INSERT INTO multi_source_kline VALUES ('2026-10-12','stock','000001','a','qfq',5),('2026-10-12','stock','000001','a','qfq',6)")
        before=con.execute('SELECT * FROM multi_source_kline').fetchall()
        with pytest.raises(RuntimeError,match='migration 16'):
            apply_pending(con,folder)
        assert con.execute('SELECT * FROM multi_source_kline').fetchall()==before
        assert con.execute('SELECT count(*) FROM schema_migration').fetchone()[0]==0
        assert con.execute('SELECT count(*) FROM schema_migration_checksum').fetchone()[0]==0


def test_history_audit_is_read_only_and_does_not_call_different_adjustments_conflicts(tmp_path):
    from scripts.audit_source_conflicts import audit, render
    db=tmp_path/'history.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,provider VARCHAR,adjustment VARCHAR,is_stale BOOLEAN)')
        con.execute("INSERT INTO multi_source_kline VALUES ('2026-10-12','stock','000001','a','none',false),('2026-10-12','stock','000001','b','qfq',false)")
        con.execute('CREATE TABLE sector_stocks(date DATE,sector_code VARCHAR,stock_code VARCHAR)')
        con.execute("INSERT INTO sector_stocks VALUES ('2026-10-12','T','000001'),('2026-10-12','T','000001')")
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    result=audit(db)
    assert result['conflicts']['kline']==[]
    items={r['id']:r for r in result['historical_worklist']['items']}
    assert items['sector_stocks_duplicates']['count']==1
    assert items['market_count_lineage']['count'] is None
    assert 'not_assessed_missing_schema' in render(result)
    assert hashlib.sha256(db.read_bytes()).hexdigest()==before
