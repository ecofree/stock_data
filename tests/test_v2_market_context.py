from copy import deepcopy
from dataclasses import replace
import json

import pytest

from trade_system.v2.context_storage import import_context, get_context
from trade_system.v2.domain import utc
from trade_system.v2.market_context import build_context, ContextPolicy, holding_risks
from trade_system.v2.storage import Store


POLICY = ContextPolicy('experimental-v1',.9,2,.5,1,10)


def fixture_bundle():
    products = {}
    for key,metric,unit in [('bars','unadjusted_daily_bar','CNY'),('memberships','dated_membership','membership'),
                            ('limits','observed_limit_pool','count'),('calendar','calendar','boolean')]:
        products[key] = dict(delivery_provider='fixture',origin_family='same_origin',source_api=key,
                             metric_definition=metric,unit=unit,priority=0,license_status='verified')
    bundle = dict(mode='system_replay',trade_date='2026-09-07',asof='2026-09-07T18:00:00+08:00',
                  products=products,expected_universe=['SH.600001','SH.600002','SZ.000003'],
                  universe_certified=True,calendar_authority_verified=True,limit_pool_complete=True,
                  bars=[],memberships=[],limits=[],calendar=[])
    def base(day,product):
        return dict(date=day,product=product,event_at=day+'T15:00:00+08:00',
                    received_at=day+'T16:00:00+08:00',known_at=day+'T16:01:00+08:00',revision='1')
    for day,prior in [('2026-09-04','2026-09-03'),('2026-09-07','2026-09-04')]:
        bundle['calendar'].append({**base(day,'calendar'),'exchange':'SSE','is_open':True,'previous_open':prior})
        for i,code in enumerate(bundle['expected_universe']):
            bundle['bars'].append({**base(day,'bars'),'instrument':code,'close':10,'change_pct':[1,2,0][i],'amount_cny':[100,200,300][i]})
            bundle['memberships'].append({**base(day,'memberships'),'instrument':code,'theme_id':'THEME-A','theme_name':'测试题材','date_verified':True})
        for code in bundle['expected_universe'][:2]:
            bundle['limits'].append({**base(day,'limits'),'instrument':code,'height':2 if day=='2026-09-07' else 1})
    return bundle


def test_observed_roles_keep_ties_capacity_and_real_calendar_cohorts():
    result = build_context(fixture_bundle(),POLICY)
    market,theme = result['market_dimensions'],result['theme_roles'][0]
    assert market['previous_open'] == '2026-09-04'  # weekend is not day-1
    assert market['promotion_fraction'] == 1
    assert market['flat'] == 1 and market['coverage'] == 1
    assert theme['height_roles'] == ['SH.600001','SH.600002']
    assert theme['capacity_roles'][0] == 'SZ.000003'
    assert theme['catalyst'] is None
    assert len(result['conditional_candidates']) == 2
    assert all(c['state']=='watch' and not c['execution_ready'] for c in result['conditional_candidates'])


def test_missing_quotes_do_not_shrink_universe_or_member_denominator():
    bundle = fixture_bundle()
    bundle['bars'] = [r for r in bundle['bars'] if not (r['date']=='2026-09-07' and r['instrument']=='SZ.000003')]
    result = build_context(bundle,POLICY)
    assert result['market_dimensions']['expected_universe'] == 3
    assert result['market_dimensions']['coverage'] == pytest.approx(2/3)
    assert result['theme_roles'][0]['quote_coverage'] == pytest.approx(2/3)
    assert result['conditional_candidates'] == []


def test_late_membership_revision_not_visible_in_system_replay():
    bundle = fixture_bundle()
    late = deepcopy(bundle['memberships'][-1])
    late.update(theme_name='后来修订',received_at='2026-09-08T16:00:00+08:00',known_at='2026-09-08T16:01:00+08:00',revision='2')
    bundle['memberships'].append(late)
    result = build_context(bundle,POLICY)
    assert result['theme_roles'][0]['name'] == '测试题材'
    assert result['diagnostics']['not_yet_known_excluded'] == 1
    bundle['mode'] = 'historical_research'
    assumed = build_context(bundle,POLICY)
    assert 'historical_availability_assumptions' in assumed['blockers']
    assert not assumed['execution_ready']
    assert assumed['input_hash'] != result['input_hash']


def test_membership_not_carried_forward_and_missing_calendar_not_guessed():
    bundle = fixture_bundle()
    bundle['memberships'] = [r for r in bundle['memberships'] if r['date'] != '2026-09-07']
    bundle['calendar'] = []
    result = build_context(bundle,POLICY)
    assert result['theme_roles'] == []
    assert result['market_dimensions']['previous_open'] is None
    assert result['market_dimensions']['promotion_fraction'] is None
    assert 'calendar_not_certified' in result['blockers']


def test_source_priority_precedes_recency_and_same_origin_not_extra_votes():
    bundle = fixture_bundle()
    bundle['products']['relay'] = {**bundle['products']['bars'],'priority':1,'delivery_provider':'relay'}
    duplicate = {**bundle['bars'][-1],'product':'relay','change_pct':99,
                 'received_at':'2026-09-07T17:00:00+08:00','known_at':'2026-09-07T17:01:00+08:00'}
    bundle['bars'].append(duplicate)
    bundle['memberships'] += deepcopy(bundle['memberships'])
    result = build_context(bundle,POLICY)
    assert result['market_dimensions']['flat'] == 1
    assert result['theme_roles'][0]['member_count'] == 3
    assert result['source_origin_families'] == ['same_origin']
    assert result['market_dimensions']['fractional_theme_hhi'] == 1


def test_same_priority_conflict_and_nonfinite_are_quarantined():
    bundle = fixture_bundle()
    bundle['bars'].append({**bundle['bars'][-1],'change_pct':'NaN'})
    bundle['bars'].append({**bundle['bars'][-2],'change_pct':10})
    result = build_context(bundle,POLICY)
    assert result['diagnostics']['bars_invalid_excluded'] == 1
    assert result['diagnostics']['bars_conflict_excluded'] == 1
    assert result['market_dimensions']['observed_quotes'] == 2


def test_non_json_float_nan_rejects_entire_envelope():
    bundle = fixture_bundle()
    bundle['bars'][0]['change_pct'] = float('nan')
    with pytest.raises(ValueError,match='JSON compliant'):
        build_context(bundle,POLICY)


def test_observation_date_cannot_follow_cutoff():
    bundle = fixture_bundle()
    bundle['trade_date'] = '2026-09-08'
    with pytest.raises(ValueError,match='query asof'):
        build_context(bundle,POLICY)


def test_nonpromoted_cohort_reports_missing_observation():
    bundle = fixture_bundle()
    bundle['limits'] = [r for r in bundle['limits'] if r['date'] != '2026-09-07']
    bundle['bars'] = [r for r in bundle['bars'] if not (r['date']=='2026-09-07' and r['instrument']=='SH.600002')]
    result = build_context(bundle,POLICY)['market_dimensions']
    assert result['nonpromoted_cohort'] == 2 and result['nonpromoted_observed'] == 1
    assert result['nonpromoted_mean_return_pct'] == 1


def test_holdings_are_prioritized_without_fabricating_sell_permission():
    context = build_context(fixture_bundle(),POLICY)
    account = {'asof':'2026-09-08T09:30:00+08:00','positions':[
        {'instrument':'SH.600001','quantity':100,'sellable':100},
        {'instrument':'SZ.000099','quantity':200,'sellable':0}]}
    risks = holding_risks(context,account)
    assert risks[0]['instrument']=='SZ.000099'
    assert 'position_quote_missing' in risks[0]['reasons']
    assert 'account_and_market_dates_differ' in risks[0]['reasons']
    assert all(r['action']=='observe_not_sell_instruction' for r in risks)


def test_context_additive_migration_idempotency_and_tamper_detection(tmp_path):
    db = tmp_path/'v2.duckdb'
    with Store(db,clock=lambda:utc('2026-09-10T10:00:00+08:00')) as store:
        store.register_product('existing','CNY','point','test','fixture')
        receipt = import_context(store,fixture_bundle(),POLICY.__dict__)
        assert import_context(store,fixture_bundle(),POLICY.__dict__) == receipt
        assert store.con.execute('SELECT count(*) FROM market_context_snapshot').fetchone()[0] == 1
        assert store.con.execute('SELECT count(*) FROM data_product').fetchone()[0] == 1
        assert get_context(store,receipt['context_id'])['market_dimensions']['coverage'] == 1
    with Store(db) as store:
        assert get_context(store,receipt['context_id'])['input_hash']
        row = store.con.execute('SELECT payload FROM market_context_snapshot').fetchone()
        changed = json.loads(row[0]); changed['execution_ready']=True
        store.con.execute('UPDATE market_context_snapshot SET payload=?',[json.dumps(changed)])
        with pytest.raises(ValueError,match='checksum'):
            get_context(store,receipt['context_id'])


@pytest.mark.parametrize('value',[float('nan'),float('inf'),0,1.01])
def test_invalid_thresholds_rejected(value):
    with pytest.raises(ValueError):
        build_context(fixture_bundle(),replace(POLICY,min_coverage=value))
