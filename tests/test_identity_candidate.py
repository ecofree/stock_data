from copy import deepcopy

import pytest

from tools.v2 import build_identity_candidate as p
from trade_system.v2.domain import identity


ALIAS = {**p.ALIAS, 'source': 'announcement'}


def fixture(days=('2025-02-14', '2025-02-17')):
    prices = []; obs = {}
    for day in days:
        original = {'stock_code': '302132', 'date': day}
        prices.append({'original': original, 'original_sha256': identity(original),
            'status': 'observed_row_unit_qualified', 'volume_shares': '100', 'turnover_cny': '1000'})
        for code in ('300114.SZ', '302132.SZ'):
            for api, values in [('daily', dict(open='10', high='11', low='9', close='10', volume_shares='100', turnover_cny='1000')),
                                ('adj_factor', {'adj_factor': '2'})]:
                obs[(api, code, day)] = {'values': values, 'source_code': code, 'date': day}
        code = '300114.SZ' if day < p.ALIAS['effective'] else '302132.SZ'
        obs[('moneyflow', code, day)] = {'values': {k: '-1.23' if k=='net_mf_amount' else '1' for k in p.MONEY_FIELDS},
            'source_code': code, 'date': day}
    return prices, obs


def test_effective_boundary_preserves_sources_and_negative_net():
    prices, obs = fixture(); before = deepcopy((prices, obs))
    rows = p.assemble(prices, obs, ALIAS)
    assert [r['effective_code'] for r in rows] == ['300114.SZ', '302132.SZ']
    assert all(r['candidate_eligible'] and r['original_price_code']=='302132' for r in rows)
    assert rows[0]['candidate_money_cny']['net_mf_amount'] == '-12300.00'
    assert all(not r['identity_qualified'] and not r['research_ready'] and not r['historical_PIT_qualified'] for r in rows)
    assert before == (prices, obs)
    assert p.join_probe(rows)['join_multiplication'] == 0


@pytest.mark.parametrize('mode,reason', [
    ('inactive', 'inactive_code_money_present_no_double_count'),
    ('missing', 'effective_code_money_missing_no_fallback'),
    ('null', 'money_fields_incomplete'), ('price', 'effective_code_price_missing_or_conflicting'),
    ('factor', 'effective_code_factor_missing_or_conflicting'), ('unit', 'price_units_unqualified')])
def test_incomplete_or_conflicting_evidence_blocks_instead_of_guessing(mode, reason):
    prices, obs = fixture(('2025-02-14',)); day = prices[0]['original']['date']
    if mode == 'inactive': obs[('moneyflow', '302132.SZ', day)] = deepcopy(obs[('moneyflow', '300114.SZ', day)])
    if mode == 'missing': del obs[('moneyflow', '300114.SZ', day)]
    if mode == 'null': obs[('moneyflow', '300114.SZ', day)]['values']['net_mf_amount'] = None
    if mode == 'price': obs[('daily', '300114.SZ', day)]['values']['volume_shares'] = '1'
    if mode == 'factor': obs[('adj_factor', '300114.SZ', day)]['values']['adj_factor'] = '3'
    if mode == 'unit': prices[0]['status'] = 'unit_unqualified'
    row = p.assemble(prices, obs, ALIAS)[0]
    assert reason in row['blocked_reasons'] and row['candidate_money_cny'] is None
    assert p.join_probe([row])['nonnull_candidate_money_rows'] == 0


def test_inactive_code_cannot_fill_missing_effective_code():
    prices, obs = fixture(('2025-02-17',)); day = '2025-02-17'
    obs[('moneyflow', '300114.SZ', day)] = obs.pop(('moneyflow', '302132.SZ', day))
    row = p.assemble(prices, obs, ALIAS)[0]
    assert len(row['blocked_reasons']) == 2 and not row['candidate_eligible']


def test_duplicate_price_date_refused_even_identical():
    prices, obs = fixture()
    with pytest.raises(ValueError, match='duplicate'): p.assemble(prices+[prices[0]], obs, ALIAS)


@pytest.mark.parametrize('change', ['date', 'source_code', 'alias', 'bound'])
def test_identity_scope_and_hash_are_not_silently_changed(change):
    prices, obs = fixture(); alias = dict(ALIAS)
    if change == 'date': prices[0]['original']['date'] = '2025-02-13'
    if change == 'source_code': prices[0]['original']['stock_code'] = '300114'
    if change == 'alias': alias['effective'] = '2025-02-18'
    if change == 'bound': prices *= 65
    with pytest.raises(ValueError): p.assemble(prices, obs, alias)


def test_equal_numeric_representation_and_amount_rounding_are_allowed():
    prices, obs = fixture(('2025-02-14',)); key = ('daily', '300114.SZ', '2025-02-14')
    obs[key]['values']['volume_shares'] = '100.000'
    obs[key]['values']['turnover_cny'] = '1000.50'
    assert p.assemble(prices, obs, ALIAS)[0]['candidate_eligible']
    obs[key]['values']['turnover_cny'] = '1000.51'
    assert not p.assemble(prices, obs, ALIAS)[0]['candidate_eligible']


def test_output_namespace_protects_evidence(tmp_path):
    source = tmp_path/'source'; source.mkdir()
    with pytest.raises(ValueError): p.separate(source/'nested', source)
    with pytest.raises(ValueError): p.separate(tmp_path, source)


def test_resealed_candidate_tampering_rejected(tmp_path, monkeypatch):
    prices, obs = fixture(); expected = {'rows': p.assemble(prices, obs, ALIAS), 'summary': {}}
    monkeypatch.setattr(p, 'payload', lambda *args: deepcopy(expected))
    folder = tmp_path/'candidate'
    p.build(tmp_path/'layer', tmp_path/'receipts', tmp_path/'db', tmp_path/'policy', folder)
    assert p.verify(folder, None, None, None, None) == expected
    changed = deepcopy(expected); changed['rows'][0]['candidate_money_cny']['net_mf_amount'] = '100000'
    path = folder/'candidate.json'; path.unlink(); p.write_json(path, changed)
    (folder/'completed.json').unlink(); p.seal(folder)
    with pytest.raises(ValueError, match='source replay'): p.verify(folder, None, None, None, None)


def test_duplicate_candidate_consumer_row_refused():
    prices, obs = fixture(); rows = p.assemble(prices, obs, ALIAS)
    with pytest.raises(p.duckdb.ConstraintException): p.join_probe(rows+[rows[0]])


def test_zero_is_distinct_from_missing_money():
    prices, obs = fixture(('2025-02-17',))
    obs[('moneyflow', '302132.SZ', '2025-02-17')]['values'] = dict.fromkeys(p.MONEY_FIELDS, '0')
    row = p.assemble(prices, obs, ALIAS)[0]
    assert row['candidate_eligible'] and row['candidate_money_cny']['net_mf_amount'] == '0'


def test_windows_are_explicit_and_not_bridged():
    prices, obs = fixture(('2024-01-02', '2025-02-17'))
    rows = p.assemble(prices, obs, ALIAS)
    assert rows[0]['observation_window'] != rows[1]['observation_window']
    prices, obs = fixture(('2024-06-03',))
    with pytest.raises(ValueError, match='observation windows'): p.assemble(prices, obs, ALIAS)
