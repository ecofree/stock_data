from __future__ import annotations

import duckdb
import json
import pytest

from trade_system.ai_review import build_ai_review_snapshot
from trade_system.deepseek_client import (
    DeepSeekReviewClient, DeepSeekReviewError, DeepSeekSettings, validate_review_output,
)


def test_ai_review_snapshot_is_deterministic_contract_without_model_call(tmp_path):
    db = tmp_path / "ai.duckdb"
    con = duckdb.connect(str(db))
    con.close()

    snapshot = build_ai_review_snapshot(db, "2026-01-02")
    assert snapshot["schema_version"] == "ai_review_facts_v1"
    assert snapshot["analysis_only"] is True
    assert snapshot["execution_ready"] is False
    assert snapshot["ai_contract"]["status"] == "facts_ready_no_model_call"
    assert "change_data_quality_or_execution_gate" in snapshot["ai_contract"]["forbidden"]


def test_deepseek_review_validates_evidence_ids_without_exposing_provider_call(tmp_path):
    db = tmp_path / "ai_evidence.duckdb"
    con = duckdb.connect(str(db))
    con.close()
    snapshot = build_ai_review_snapshot(db, "2026-01-02")
    evidence_id = snapshot["evidence_catalog"][0]["evidence_id"]

    def fake_read(request, **limits):
        assert request.get_method() == 'POST'
        assert limits == {'timeout': 10, 'max_bytes': 1_000_000}
        return json.dumps({'model': 'deepseek-v4-flash', 'choices': [{'message': {'content': json.dumps({
            'summary': 'ok', 'risk_flags': [], 'watchlist': [], 'evidence_ids': [evidence_id]})}}],
            'usage': {'total_tokens': 1}}).encode()

    client = DeepSeekReviewClient(
        DeepSeekSettings(api_key="test", base_url="https://example.test", model="deepseek-v4-flash", timeout=10),
        read=fake_read,
    )
    result = client.review(snapshot)
    assert result["status"] == "success"
    assert result["review"]["summary"] == "ok"


def test_deepseek_review_rejects_unknown_evidence(tmp_path):
    db = tmp_path / "ai_bad_evidence.duckdb"
    con = duckdb.connect(str(db))
    con.close()
    snapshot = build_ai_review_snapshot(db, "2026-01-02")

    def fake_read(*args, **kwargs):
        return json.dumps({'choices': [{'message': {'content': json.dumps({
            'summary': 'x', 'risk_flags': [], 'watchlist': [], 'evidence_ids': ['unknown']})}}]}).encode()

    client = DeepSeekReviewClient(
        DeepSeekSettings(api_key="test", base_url="https://example.test", timeout=10),
        read=fake_read,
    )
    try:
        client.review(snapshot)
    except DeepSeekReviewError as exc:
        assert "unknown evidence_ids" in str(exc)
    else:
        raise AssertionError("unknown evidence id was accepted")


@pytest.mark.parametrize('failure', [TimeoutError('secret fixture credential'), ValueError('bad json')])
def test_ai_provider_failure_is_single_attempt_and_sanitized(failure):
    calls = []
    def read(request, **limits):
        from trade_system.http_transport import request_deadline
        assert request_deadline.get() is not None
        calls.append(request)
        raise failure
    client = DeepSeekReviewClient(DeepSeekSettings(api_key='synthetic', base_url='https://fixture.invalid'), read=read)
    with pytest.raises(DeepSeekReviewError, match='provider request or response invalid') as exc:
        client.review({'evidence_catalog': []})
    assert 'secret' not in str(exc.value) and len(calls) == 1


def test_ai_expired_shared_budget_does_not_send_facts():
    import time
    from trade_system.http_transport import request_deadline
    def forbidden(*a, **kw):
        pytest.fail('expired request must not send facts')
    token = request_deadline.set(time.monotonic()-1)
    try:
        client = DeepSeekReviewClient(DeepSeekSettings(api_key='synthetic', base_url='https://fixture.invalid'), read=forbidden)
        with pytest.raises(DeepSeekReviewError):
            client.review({})
    finally:
        request_deadline.reset(token)


@pytest.mark.parametrize('price_ready', [True, False])
def test_unconfirmed_flow_is_not_supplied_through_other_ai_evidence(monkeypatch, tmp_path, price_ready):
    context = {
        'trade_date': '2026-09-29',
        'readiness': {
            'data_certified_ready': True, 'flow_certified_ready': False,
            'analysis_ready': False,
            'capital_flow_health': {'unconfirmed_raw_amount': 'unconfirmed-flow-sentinel'},
        },
        'operational_capabilities': {
            'schema': 'operational_capabilities_v1',
            'capabilities': {'market_view': {'ready': price_ready,
                'breadth': {'rise': 3, 'fall': 2, 'flat': 0, 'samples': 5},
                'breadth_scope': 'exact_qualified_price_subset_not_exchange_total'}},
        },
        'regime': {'funds_score': 'unconfirmed-flow-sentinel'},
        'market_context': {'breadth': [{'rise': 999, 'fall': 999}], 'auction': [{'volume': 3}]},
        'capital_flow': {
            'stock_inflow': [{'stock_code': '000001', 'main_net': 'unconfirmed-flow-sentinel'}],
            'candidate_picks': [{'stock_code': '000001', 'money_score': 'unconfirmed-flow-sentinel'}],
        },
        'data_sources': {'flow_features': {'score': 'unconfirmed-flow-sentinel'}},
    }
    monkeypatch.setattr('trade_system.ai_review.build_daily_review_context', lambda *a: context)
    snapshot = build_ai_review_snapshot(tmp_path / 'not_opened.duckdb', '2026-09-29')
    assert 'unconfirmed-flow-sentinel' not in json.dumps(snapshot)
    assert snapshot['capital_flow'] == {} and snapshot['research'] == {}
    assert snapshot['market']['breadth'] == (
        context['operational_capabilities']['capabilities']['market_view']['breadth'] if price_ready else {})
    assert snapshot['ai_contract']['scope'] == ('price_observation' if price_ready else 'data_limitations_only')
    assert snapshot['ai_contract']['watchlist_allowed'] is False
    assert not any(item['evidence_id'].startswith(('flow.', 'research.')) for item in snapshot['evidence_catalog'])


@pytest.mark.parametrize('evidence', ['flow.stock_inflow_top50', 'market.regime', 'research.candidates'])
def test_existing_evidence_id_cannot_restore_unconfirmed_ai_domain(evidence):
    snapshot = {
        'data_gate': {'data_certified_ready': True, 'flow_certified_ready': False, 'analysis_ready': False},
        'ai_contract': {'scope': 'certified_analysis', 'watchlist_allowed': True},
        'evidence_catalog': [{'evidence_id': evidence, 'usage': ['summary', 'watchlist']}],
    }
    output = {'summary': 'invalid domain', 'risk_flags': [], 'watchlist': [], 'evidence_ids': [evidence]}
    with pytest.raises(DeepSeekReviewError, match='unknown evidence_ids'):
        validate_review_output(output, snapshot)


def test_data_gate_is_not_individual_signal_evidence_even_when_analysis_is_certified():
    snapshot = {
        'data_gate': {'data_certified_ready': True, 'flow_certified_ready': True, 'analysis_ready': True},
        'ai_contract': {'scope': 'certified_analysis', 'watchlist_allowed': True},
        'evidence_catalog': [{'evidence_id': 'data_gate', 'usage': ['summary', 'risk_flags']}],
    }
    output = {'summary': 'review', 'risk_flags': [],
              'watchlist': [{'stock_code': '000001', 'evidence_ids': ['data_gate']}],
              'evidence_ids': ['data_gate']}
    with pytest.raises(DeepSeekReviewError, match='not qualified for that use'):
        validate_review_output(output, snapshot)


def test_price_facts_can_be_summarized_without_granting_a_watchlist():
    snapshot = {
        'data_gate': {'data_certified_ready': True, 'flow_certified_ready': False, 'analysis_ready': False},
        'operational_capabilities': {'schema': 'operational_capabilities_v1',
                                     'capabilities': {'market_view': {'ready': True, 'breadth': {'rise': 3, 'fall': 2}}}},
        'evidence_catalog': [{'evidence_id': 'market.breadth', 'usage': ['summary', 'risk_flags']}],
    }
    output = {'summary': '3 rise, 2 fall', 'risk_flags': [], 'watchlist': [], 'evidence_ids': ['market.breadth']}
    assert validate_review_output(output, snapshot) is output
    output['watchlist'] = [{'stock_code': '000001', 'evidence_ids': ['market.breadth']}]
    with pytest.raises(DeepSeekReviewError, match='without qualified signal evidence'):
        validate_review_output(output, snapshot)


@pytest.mark.parametrize('contract', [None, {'scope': 'price_observation', 'watchlist_allowed': False},
                                      {'scope': 'certified_analysis', 'watchlist_allowed': True}])
def test_conflicting_or_legacy_ai_contract_cannot_restore_signal_observations(contract):
    snapshot = {
        'data_gate': {'data_certified_ready': True, 'flow_certified_ready': True, 'analysis_ready': True,
                      'gate': {'data_certified_ready': False, 'flow_certified_ready': False,
                               'analysis_ready': False}},
        'evidence_catalog': [{'evidence_id': 'data_gate', 'usage': ['summary', 'risk_flags']}],
    }
    if contract is not None:
        snapshot['ai_contract'] = contract
    output = {'summary': 'qualified limitations only', 'risk_flags': [], 'watchlist': [],
              'evidence_ids': ['data_gate']}
    assert validate_review_output(output, snapshot) is output
    output['stock_observations'] = [{'stock_code': '000001', 'reason': 'unqualified signal'}]
    with pytest.raises(DeepSeekReviewError, match='signal observations unavailable'):
        validate_review_output(output, snapshot)
