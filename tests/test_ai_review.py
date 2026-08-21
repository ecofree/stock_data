from __future__ import annotations

import duckdb
from types import SimpleNamespace

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

    def fake_post(*args, **kwargs):
        return SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "model": "deepseek-v4-flash",
                "choices": [{
                    "message": {"content": '{"summary":"ok","risk_flags":[],"watchlist":[],"evidence_ids":["%s"]}' % evidence_id}
                }],
                "usage": {"total_tokens": 1},
            },
        )

    client = DeepSeekReviewClient(
        DeepSeekSettings(api_key="test", base_url="https://example.test", model="deepseek-v4-flash", max_retries=0),
        post=fake_post,
        sleep=lambda _: None,
    )
    result = client.review(snapshot)
    assert result["status"] == "success"
    assert result["review"]["summary"] == "ok"


def test_deepseek_review_rejects_unknown_evidence(tmp_path):
    db = tmp_path / "ai_bad_evidence.duckdb"
    con = duckdb.connect(str(db))
    con.close()
    snapshot = build_ai_review_snapshot(db, "2026-01-02")

    def fake_post(*args, **kwargs):
        return SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": '{"summary":"x","risk_flags":[],"watchlist":[],"evidence_ids":["unknown"]}'}}]},
        )

    client = DeepSeekReviewClient(
        DeepSeekSettings(api_key="test", base_url="https://example.test", max_retries=0),
        post=fake_post,
        sleep=lambda _: None,
    )
    try:
        client.review(snapshot)
    except DeepSeekReviewError as exc:
        assert "unknown evidence_ids" in str(exc)
    else:
        raise AssertionError("unknown evidence id was accepted")
