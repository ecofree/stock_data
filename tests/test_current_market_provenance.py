"""Read stored market facts independently of the collectors that wrote them."""
from datetime import datetime, timedelta
import json

import duckdb
import pytest

from trade_system.market_contract import MARKET_COUNT_FIELDS, count_evidence
from trade_system.normalize import _create_market_daily, _create_market_state_inputs
from trade_system.readiness import relation_freshness, assess_trade_date_readiness
from trade_system.review_queries import _market_context_review
from trade_system.review_web import _render_kpis

DAY = '2026-10-12'
RECEIVED = datetime(2026, 10, 12, 16)
AS_OF = RECEIVED + timedelta(minutes=1)
COUNTS = (10, 2, 3000, 1000, 3)


def evidence(counts=COUNTS):
    return {field: count_evidence(value, source='kpl_daily', source_kind='real',
                source_date=DAY, fetched_at=RECEIVED, received_at=RECEIVED)
            for field, value in zip(MARKET_COUNT_FIELDS, counts)}


def create(con, *, kind='real', proof=None, counts=COUNTS, row_clock=RECEIVED, include_proof=True):
    con.execute('CREATE TABLE daily_summary(date DATE,limit_up_count DOUBLE,limit_down_count DOUBLE,'
                'rise_count DOUBLE,fall_count DOUBLE,consecutive_count DOUBLE,source_kind VARCHAR,fetched_at TIMESTAMP'
                + (',count_provenance_json VARCHAR' if include_proof else '') + ')')
    values = [DAY, *counts, kind, row_clock]
    if include_proof:
        values.append(json.dumps(proof) if isinstance(proof, dict) else proof)
    con.execute('INSERT INTO daily_summary VALUES (' + ','.join('?' for _ in values) + ')', values)
    _create_market_daily(con)
    _create_market_state_inputs(con)


def assert_unqualified(con, *, as_of=AS_OF):
    for relation in ('daily_summary', 'v_market_daily', 'v_market_state_inputs'):
        actual = relation_freshness(con, relation, DAY, now=as_of)
        assert actual['status'] != 'ready', (relation, actual)
        assert actual['market_daily_status'] != 'available', (relation, actual)
        assert actual['real_rows'] == 0
    assert not assess_trade_date_readiness(con, DAY, required_groups=('market_state',), now=as_of)['groups'][0]['ready']


@pytest.mark.parametrize('kind', [None, '', 'unknown', 'unverified', 'provider_says_real'])
def test_unknown_row_identity_cannot_borrow_complete_receipts(kind):
    with duckdb.connect(':memory:') as con:
        create(con, kind=kind, proof=evidence())
        assert_unqualified(con)
        assert con.execute('SELECT source_kind FROM daily_summary').fetchone() == (kind,)


@pytest.mark.parametrize('proof', [None, 'invalid', '{}', '[]', 'null', '"real"', ' ' * 8193])
def test_missing_or_corrupt_stored_provenance_cannot_borrow_real_label(proof):
    with duckdb.connect(':memory:') as con:
        create(con, proof=proof)
        assert_unqualified(con)
        assert con.execute('SELECT count_provenance_json FROM daily_summary').fetchone() == (proof,)
        assert con.execute('SELECT earning_effect_score,acute_drop_risk_score FROM v_market_state_inputs').fetchone() == (None, None)
        context = _market_context_review(con, DAY)
        assert context['market_daily']['market_daily_status'] == 'unverified'
        assert '五项计数完整' not in _render_kpis({'market_context': context})


@pytest.mark.parametrize('damage', [
    'missing_field', 'value', 'source', 'source_kind', 'source_date', 'missing_time',
    'invalid_time', 'future_time', 'offset_time', 'qualified', 'basis', 'refreshed_row_clock',
])
def test_each_constituent_is_requalified_from_stored_facts(damage):
    proof = evidence()
    row_clock = RECEIVED
    item = proof['fall_count']
    if damage == 'missing_field':
        del proof['fall_count']
    elif damage == 'refreshed_row_clock':
        row_clock += timedelta(seconds=1)
    else:
        key, value = {
            'value': ('value', 99),
            'source': ('source', 'unknown'),
            'source_kind': ('source_kind', 'fallback'),
            'source_date': ('source_date', '2026-10-11'),
            'missing_time': ('fetched_at', None),
            'invalid_time': ('fetched_at', 'bad time'),
            'future_time': ('fetched_at', (AS_OF + timedelta(seconds=1)).isoformat()),
            'offset_time': ('fetched_at', RECEIVED.isoformat() + '+08:00'),
            'qualified': ('qualified', False),
            'basis': ('freshness_basis', 'row_rewrite_time'),
        }[damage]
        item[key] = value
    with duckdb.connect(':memory:') as con:
        create(con, proof=proof, row_clock=row_clock)
        assert_unqualified(con)


@pytest.mark.parametrize('kind', ['real', 'real_cross_source', 'secondary_verified'])
@pytest.mark.parametrize('counts', [COUNTS, (0, 0, 0, 0, 0)])
def test_known_observation_and_explicit_zero_with_complete_receipts_still_pass(kind, counts):
    proof = evidence(counts)
    with duckdb.connect(':memory:') as con:
        create(con, kind=kind, proof=proof, counts=counts)
        for relation in ('daily_summary', 'v_market_daily', 'v_market_state_inputs'):
            actual = relation_freshness(con, relation, DAY, now=AS_OF)
            assert actual['status'] == 'ready', (relation, actual)
            assert actual['market_daily_status'] == 'available'
        raw = con.execute('SELECT count_provenance_json FROM daily_summary').fetchone()[0]
        assert con.execute('SELECT count_provenance_json FROM v_market_daily').fetchone()[0] == raw
        assert con.execute('SELECT count_provenance_json FROM v_market_state_inputs').fetchone()[0] == raw
        assert '五项计数完整' in _render_kpis({'market_context': _market_context_review(con, DAY)})


@pytest.mark.parametrize('kind', ['real', 'real_cross_source', 'secondary_verified'])
def test_legacy_schema_and_aliases_do_not_invent_missing_proof(kind):
    with duckdb.connect(':memory:') as con:
        create(con, kind=kind, include_proof=False)
        assert_unqualified(con)
        assert con.execute('SELECT limit_down_count FROM daily_summary').fetchone() == (2,)


@pytest.mark.parametrize('level', ['field', 'entry'])
def test_duplicate_receipt_keys_are_ambiguous_even_if_first_value_looks_valid(level):
    proof = json.dumps(evidence())
    if level == 'field':
        proof = '{"fall_count":' + json.dumps(evidence()['fall_count']) + ',' + proof[1:]
    else:
        proof = proof.replace('"qualified": true', '"qualified": true, "qualified": false', 1)
    with duckdb.connect(':memory:') as con:
        create(con, proof=proof)
        assert_unqualified(con)


def test_legacy_varchar_row_clock_cannot_silently_drop_a_timezone():
    with duckdb.connect(':memory:') as con:
        create(con, proof=evidence())
        con.execute('DROP VIEW v_market_state_inputs')
        con.execute('DROP VIEW v_market_daily')
        con.execute('ALTER TABLE daily_summary ALTER fetched_at TYPE VARCHAR')
        con.execute("UPDATE daily_summary SET fetched_at='2026-10-12T16:00:00+02:00'")
        _create_market_daily(con)
        _create_market_state_inputs(con)
        assert_unqualified(con)


def test_receipt_is_rechecked_at_reader_as_of_not_only_when_view_was_created():
    with duckdb.connect(':memory:') as con:
        proof = evidence()
        proof['fall_count']['fetched_at'] = (RECEIVED + timedelta(seconds=30)).isoformat()
        create(con, proof=proof)
        assert con.execute('SELECT market_daily_status FROM v_market_daily').fetchone() == ('available',)
        assert_unqualified(con, as_of=RECEIVED + timedelta(seconds=15))
        assert relation_freshness(con, 'v_market_daily', DAY, now=AS_OF)['status'] == 'ready'


def test_forged_projection_status_without_receipts_is_not_used_for_scores():
    with duckdb.connect(':memory:') as con:
        create(con, proof=evidence())
        con.execute("UPDATE daily_summary SET count_provenance_json='{}'")
        assert con.execute('SELECT market_daily_status FROM v_market_state_inputs').fetchone() == ('unverified',)
        assert_unqualified(con)
