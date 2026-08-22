"""Tests for the daily screener scoring and LLM annotation parsing."""
from __future__ import annotations

import pytest

from trade_system.stock_screener import (
    build_llm_prompt,
    parse_llm_annotations,
    score_candidates,
)


def _row(code, board=1, qlib=None, flow=None, seal=100.0, max_seal=200.0,
         opens=0, heat=0.5):
    return {"stock_code": code, "stock_name": code, "board": board,
            "qlib_score": qlib, "flow_rank_pct": flow, "seal_money": seal,
            "max_seal_money": max_seal, "open_times": opens,
            "concept_heat": heat, "limit_up_reason": "测试"}


def test_score_is_deterministic_and_sorted():
    rows = [_row("A", qlib=0.9, flow=0.8), _row("B", qlib=0.2, flow=0.1)]
    p1 = score_candidates([*rows], phase="recovery")
    p2 = score_candidates([_row("B", qlib=0.2, flow=0.1),
                           _row("A", qlib=0.9, flow=0.8)], phase="recovery")
    assert [p["stock_code"] for p in p1] == ["A", "B"]
    assert [p["stock_code"] for p in p2] == ["A", "B"]  # order-independent
    assert p1[0]["total_score"] > p1[1]["total_score"]


def test_high_board_penalty_in_climax():
    rows = [_row("HIGH", board=4, qlib=0.5, flow=0.5),
            _row("LOW", board=1, qlib=0.5, flow=0.5)]
    climax = {p["stock_code"]: p["total_score"]
              for p in score_candidates(rows, "climax")}
    recovery = {p["stock_code"]: p["total_score"]
                for p in score_candidates(rows, "recovery")}
    # In climax the 4-board stock is penalized relative to first board.
    assert (climax["HIGH"] - climax["LOW"]) < (recovery["HIGH"] - recovery["LOW"])
    assert recovery["LOW"] > climax["LOW"]  # first-board bonus in recovery


def test_seal_maintenance_ratio_beats_raw_size():
    # A keeps only 20% of peak but huge absolute; B keeps 90% small.
    rows = [_row("A", seal=500.0, max_seal=2500.0, qlib=0.5, flow=0.5),
            _row("B", seal=90.0, max_seal=100.0, qlib=0.5, flow=0.5)]
    ranked = {p["stock_code"]: p["rank"]
              for p in score_candidates(rows, "divergence")}
    assert ranked["B"] < ranked["A"]  # better maintenance ranks higher


def test_open_times_penalty_applies():
    a = score_candidates([_row("CLEAN", opens=0)], "divergence")[0]
    b = score_candidates([_row("REOPEN", opens=3)], "divergence")[0]
    assert b["total_score"] == pytest.approx(a["total_score"] - 3.0)


def test_factor_json_is_auditable():
    p = score_candidates([_row("X", qlib=0.6, flow=0.4)], "ice")[0]
    fj = p["factor_json"]
    assert set(fj) >= {"qlib", "flow", "seal", "heat", "phase_adjust"}
    assert isinstance(fj["notes"], str)


def test_parse_llm_annotations_tolerates_fences_and_noise():
    raw = '```json\n[{"code":"600519","bull_case":"题材共振","risk":"高开过大",' \
          '"watch_condition":"低开3%内可看"}]\n```'
    out = parse_llm_annotations(raw)
    assert out["600519"]["bull_case"] == "题材共振"
    assert parse_llm_annotations("模型胡言乱语没有json") == {}
    out2 = parse_llm_annotations('前缀文字 [ {"code":"1","bull_case":"x"} ] 后缀')
    assert out2["1"]["bull_case"] == "x"


def test_prompt_contains_phase_and_evidence():
    prompt = build_llm_prompt("retreat", [_row("Z", board=2)])
    assert "retreat" in prompt and '"code":"Z"' in prompt
