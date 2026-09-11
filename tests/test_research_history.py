import json
import statistics

import duckdb
import pytest

from scripts.export_qlib_features import _query, export_features
from tests.test_research_receipts import calendar, Fixture, NOW
from trade_system.v2 import research_receipts as rr
from trade_system.v2 import research_history as rh
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import file_hash, identity
from tools.v2.capture_research_history import extension_plan


def batches(tmp_path, client=None):
    c = calendar(tmp_path)
    one = tmp_path/'one'; two = tmp_path/'two'
    rr.capture(c, '2024-01-02', '2024-01-05', one, client=Fixture(), clock=lambda: NOW)
    rr.capture(c, '2024-01-08', '2024-01-17', two, client=client or Fixture(), clock=lambda: NOW)
    return one, two


def test_collection_deduplicates_exact_overlaps_and_binds_children(tmp_path):
    paths = batches(tmp_path); out = tmp_path/'collection'
    meta = rh.create_collection(paths, out)
    reg, data, checked = rr.verify(out)
    assert meta == checked and reg['end'] == '2024-01-17'
    assert len(reg['days']) == 14 and len(data['adj_factor']) == 14
    assert meta['overlap_api_days_verified'] == 4
    assert meta['origin'] == 'synthetic_fixture' and meta['research_ready'] is False
    rec = paths[1]/'adj_factor-2024-01-08.json'
    rec.write_text('{}')
    with pytest.raises(ValueError, match='changed'): rr.verify(out)


@pytest.mark.parametrize('api', ['adj_factor', 'moneyflow'])
def test_overlapping_revision_is_rejected(tmp_path, api):
    class Changed(Fixture):
        def query(self, requested, day):
            data = super().query(requested, day)
            if requested == api: data['items'][0][2] = 5
            return data
    paths = batches(tmp_path, Changed())
    with pytest.raises(ValueError, match='revision conflict'): rh.combine(paths)


@pytest.mark.parametrize('kind', ['duplicate', 'reverse', 'nested', 'origin', 'calendar', 'budget', 'summary'])
def test_reject_invalid_collection(tmp_path, kind):
    paths = list(batches(tmp_path)); out = tmp_path/'collection'
    if kind == 'duplicate': paths[1] = paths[0]
    elif kind == 'reverse': paths.reverse()
    elif kind == 'nested':
        rh.create_collection(paths, out); paths = [out]
    elif kind in ('origin', 'calendar', 'budget'):
        regpath = paths[1]/'registration.json'
        value = json.loads(regpath.read_text())
        key = {'origin': 'origin', 'calendar': 'calendar_manifest_id', 'budget': 'max_requests'}[kind]
        value[key] = {'origin': 'xiaodefa_relay', 'calendar': 'wrong', 'budget': 999}[kind]
        regpath.write_text(json.dumps(value))
        reportpath = paths[1]/'report.json'; report = json.loads(reportpath.read_text())
        report['registration_id'] = identity(value); report['origin'] = value['origin']
        reportpath.write_text(json.dumps(report))
        (paths[1]/'completed.json').unlink(); seal(paths[1])
    elif kind == 'summary':
        rh.create_collection(paths, out)
        indexpath = out/'collection.json'; index = json.loads(indexpath.read_text())
        index['summary']['research_ready'] = True; indexpath.write_text(json.dumps(index))
        (out/'completed.json').unlink(); seal(out)
        with pytest.raises(ValueError, match='derivation changed'): rr.verify(out)
        return
    with pytest.raises(ValueError): rh.combine(paths)


def test_extension_plan_keeps_ten_session_budget_and_exact_tail():
    from datetime import date, timedelta
    days = [(date(2024, 1, 1)+timedelta(days=i)).isoformat() for i in range(40)]
    plan = extension_plan(days, days[:6], 32)
    assert [len(p['days']) for p in plan] == [10, 10, 10, 4]
    assert sum(len(p['days'])*2 for p in plan) == 68
    assert plan[-1]['end'] == days[29]
    with pytest.raises(ValueError): extension_plan(days, days[:6], 33)
    with pytest.raises(ValueError): extension_plan(days[:10], days[:6], 32)
    with pytest.raises(ValueError): extension_plan(days, [], 32)


@pytest.mark.parametrize('gap', [False, True])
def test_repaired_full_price_windows_not_partial_or_gap_spanning(tmp_path, gap):
    from datetime import date, timedelta
    days = [(date(2024, 1, 1)+timedelta(days=i)).isoformat() for i in range(25)]
    with duckdb.connect() as c:
        c.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE)')
        c.execute('CREATE TABLE tushare_daily_basic(date DATE,stock_code VARCHAR,turnover_rate DOUBLE,volume_ratio DOUBLE,pe DOUBLE,pb DOUBLE,total_mv DOUBLE,circ_mv DOUBLE)')
        c.execute('CREATE TABLE verified_adjustment(date DATE,stock_code VARCHAR,adj_factor DOUBLE)')
        c.execute('CREATE TABLE verified_moneyflow(date DATE,stock_code VARCHAR,net_mf_amount DOUBLE,buy_lg_amount DOUBLE,sell_lg_amount DOUBLE,buy_elg_amount DOUBLE,sell_elg_amount DOUBLE)')
        c.execute('CREATE TABLE verified_calendar_overlay(cal_date DATE,is_open BOOLEAN)')
        for i, day in enumerate(days):
            c.execute('INSERT INTO verified_calendar_overlay VALUES (?,true)', [day])
            if gap and i == 10: continue
            c.execute("INSERT INTO tushare_daily VALUES (?,'000001',10,12,9,?, ?,1000)", [day, 10+i, 100+i*2])
            c.execute("INSERT INTO verified_adjustment VALUES (?,'000001',2)", [day])
            c.execute("INSERT INTO verified_moneyflow VALUES (?,'000001',10,1,1,1,1)", [day])
        frame = c.execute(_query(days[0], days[-1], include_calendar=True, include_adjustment=True,
            calendar_relation='verified_calendar_overlay', adjustment_relation='verified_adjustment',
            money_relation='verified_moneyflow')).fetchdf()
        assert frame.iloc[:19]['volume_z20'].isna().all()
        assert frame.iloc[:5]['volatility_5d'].isna().all()
        if gap:
            assert frame['volume_z20'].isna().all()
            assert frame.iloc[10]['ret_1d'] != frame.iloc[10]['ret_1d']
            assert frame.iloc[10:15]['ret_5d'].isna().all()
        else:
            volumes = [50+i for i in range(20)]
            assert abs(frame.iloc[19]['volume_z20']-(volumes[-1]-statistics.mean(volumes))/statistics.stdev(volumes)) < 1e-12
            assert frame.iloc[19]['warmup_price_observations_20d'] == 20
            assert frame.iloc[19]['warmup_price_contiguous_20d']


def test_export_consumes_pre_start_warmup_and_independent_grid(tmp_path, monkeypatch):
    from pathlib import Path
    from tools.v2.probe_research_history import run
    paths = list(batches(tmp_path)); cal = tmp_path/'calendar'
    days = json.loads((cal/'calendar-overlay.json').read_text())['open_days']
    days = [d for d in days if d >= '2024-01-02'][:32]
    for i, plan in enumerate(extension_plan(days, days[:14], 32)):
        path = tmp_path/f'more-{i}'
        rr.capture(cal, plan['start'], plan['end'], path, client=Fixture(), clock=lambda: NOW)
        paths.append(path)
    collection = tmp_path/'collection'; rh.create_collection(paths, collection)
    original = rr.verify
    def fixture_origin(p, **kwargs):
        reg, data, meta = original(p, **kwargs)
        if Path(p).resolve() == collection.resolve(): reg = {**reg, 'origin': 'xiaodefa_relay'}
        return reg, data, meta
    # This test-only origin injection is not a native data acceptance artifact.
    monkeypatch.setattr(rr, 'verify', fixture_origin)
    db = tmp_path/'prices.db'
    with duckdb.connect(str(db)) as c:
        c.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE)')
        c.execute('CREATE TABLE tushare_daily_basic(date DATE,stock_code VARCHAR,turnover_rate DOUBLE,volume_ratio DOUBLE,pe DOUBLE,pb DOUBLE,total_mv DOUBLE,circ_mv DOUBLE)')
        c.execute('CREATE TABLE tushare_trade_cal(cal_date DATE,is_open BOOLEAN)')
        c.execute("CREATE TABLE qlib_stock_flow_features_v2 AS SELECT 'unqualified' AS unusable")
        for i, day in enumerate(days):
            c.execute("INSERT INTO tushare_daily VALUES (?,'000001',?,100,1,?, ?,1000)", [day,10+i,11+i,100+i])
            c.execute('INSERT INTO tushare_trade_cal VALUES (?,true)', [day])
    before = file_hash(db)
    meta = export_features(db, tmp_path/'features.parquet', start_date=days[20], end_date=days[29],
                           output_format='parquet', calendar_overlay=cal, research_overlay=collection)
    assert meta['rows'] == 10 and meta['labeled_rows'] == 10
    assert not meta['flow_features_available'] and meta['flow_feature_versions'] == []
    result = run(db, collection, tmp_path/'features.current.json', tmp_path/'probe')
    assert result['volume_z20_rows'] == 10 and result['cohort_changes'] == 0
    assert result['full_price_warmup_rows'] == 10 and result['moneyflow_5d_rows'] == 10
    assert before == file_hash(db)
    from tests.test_research_semantics import policy
    from tools.v2.probe_research_semantics import run as semantic_probe
    sempath,rules=policy(tmp_path)
    rules['aliases'][0]['new']='000001';rules['aliases'][0]['old']='000002'
    sempath.write_text(json.dumps(rules))
    sm=export_features(db,tmp_path/'semantic.parquet',start_date=days[20],end_date=days[29],
                      output_format='parquet',calendar_overlay=cal,research_overlay=collection,semantics=sempath)
    assert sm['labeled_rows']==0 and sm['rows']==10
    assert sm['research_semantics']['output_label_status_counts']=={'identity_conflict_unverified_continuity':10}
    assert sum(sm['research_semantics']['context_label_status_counts'].values())==32
    assert not any('target' in f or 'suspended' in f or 'identity' in f for f in sm['feature_columns'])
    proof=semantic_probe(db,sempath,tmp_path/'features.current.json',tmp_path/'semantic.current.json',tmp_path/'semantic-proof')
    assert proof['excluded_labels']==10 and proof['feature_changes']==0
