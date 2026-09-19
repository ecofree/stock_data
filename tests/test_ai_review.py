from __future__ import annotations

import duckdb
import json
import pytest

from trade_system.ai_review import build_ai_review_snapshot
from trade_system.deepseek_client import DeepSeekReviewClient, DeepSeekReviewError, DeepSeekSettings


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
