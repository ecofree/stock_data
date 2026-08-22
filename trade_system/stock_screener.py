"""Daily next-day stock screener: rule scoring + QLib score + LLM narrative.

Pipeline (v1, research-only):
  universe   : today's official limit-up pool (tomorrow's candidates)
  factors    : qlib score / flow rank / seal quality / concept heat
  phase fit  : simple board-height adjustments per emotion phase
  LLM review : DeepSeek writes bull-case / risk / watch-condition for Top N
               (narrative layer only — it never changes the ranking)

The combined score is a deterministic weighted sum; every factor is stored
in ``factor_json`` so the weighting can be audited and retuned later.
"""
from __future__ import annotations

import json
from typing import Any

# factor weights (sum = 100 before phase adjustment)
WEIGHTS = {
    "qlib": 30,
    "flow": 20,
    "seal": 20,
    "heat": 15,
}

PHASE_BOARD_ADJUST = {
    # phase -> (board threshold, penalty, first-board bonus)
    "climax": (3, -10.0, 0.0),
    "ferment": (4, -6.0, 2.0),
    "divergence": (3, -4.0, 2.0),
    "recovery": (None, 0.0, 5.0),
    "retreat": (None, -8.0, 3.0),
    "ice": (None, 0.0, 8.0),
}


def _percentile_ranks(values: list[float | None]) -> list[float]:
    """Rank-normalize to 0..1 (higher is better); None → 0."""
    indexed = [(i, v) for i, v in enumerate(values) if v is not None]
    out = [0.0] * len(values)
    if not indexed:
        return out
    ranked = sorted(indexed, key=lambda iv: iv[1])
    denom = max(1, len(ranked) - 1)
    for pos, (i, _) in enumerate(ranked):
        out[i] = pos / denom
    return out


def score_candidates(rows: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    """Combine normalized factors into total_score (deterministic).

    Expected row keys:
      stock_code, stock_name, limit_up_reason, board,
      qlib_score (float|None), flow_rank_pct (float|None, 0..1 higher=better),
      seal_money (float|None), max_seal_money (float|None), open_times (int|None),
      concept_heat (float|None 0..1)
    """
    q_qlib = _percentile_ranks([r.get("qlib_score") for r in rows])
    q_flow = _percentile_ranks([r.get("flow_rank_pct") for r in rows])
    # seal quality: maintenance ratio (current/peak) blended with absolute size
    seal_ratio = [
        (r["seal_money"] / r["max_seal_money"])
        if r.get("seal_money") is not None and r.get("max_seal_money")
        else None
        for r in rows
    ]
    q_seal_ratio = _percentile_ranks(seal_ratio)
    q_seal_abs = _percentile_ranks([r.get("seal_money") for r in rows])
    q_seal = [
        0.6 * a + 0.4 * b for a, b in zip(q_seal_ratio, q_seal_abs)
    ]
    q_heat = _percentile_ranks([r.get("concept_heat") for r in rows])

    thr, penalty, first_bonus = PHASE_BOARD_ADJUST.get(
        phase, (None, 0.0, 0.0))

    picks: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        board = r.get("board") or 1
        opens = r.get("open_times")
        factor_scores = {
            "qlib": round(q_qlib[i] * WEIGHTS["qlib"], 2),
            "flow": round(q_flow[i] * WEIGHTS["flow"], 2),
            "seal": round(q_seal[i] * WEIGHTS["seal"], 2),
            "heat": round(q_heat[i] * WEIGHTS["heat"], 2),
        }
        total = sum(factor_scores.values())
        notes: list[str] = []
        if thr is not None and board >= thr:
            total += penalty
            notes.append(f"{board}板高位相位惩罚{penalty:+.0f}")
        if board == 1 and first_bonus:
            total += first_bonus
            notes.append(f"低位首板相位加分{first_bonus:+.0f}")
        if isinstance(opens, int) and opens >= 2:
            total -= 3.0
            notes.append(f"开板{opens}次扣分-3")
        picks.append({
            **r,
            "factor_json": {**factor_scores,
                            "phase_adjust": round(total - sum(factor_scores.values()), 2),
                            "notes": "; ".join(notes)},
            "total_score": round(total, 2),
        })
    picks.sort(key=lambda p: p["total_score"], reverse=True)
    for rank, p in enumerate(picks, start=1):
        p["rank"] = rank
    return picks


def build_llm_prompt(phase: str, picks: list[dict[str, Any]]) -> str:
    """One batched prompt for the Top picks; returns strict JSON array."""
    compact = [
        {
            "code": p["stock_code"],
            "name": p.get("stock_name"),
            "board": p.get("board"),
            "reason": p.get("limit_up_reason"),
            "seal_ratio": (
                round(p["seal_money"] / p["max_seal_money"], 2)
                if p.get("seal_money") and p.get("max_seal_money") else None
            ),
            "concept_heat": p.get("concept_heat"),
            "qlib_rank_in_universe": p.get("rank"),
        }
        for p in picks[:12]
    ]
    return (
        f"当前市场情绪相位：{phase}。以下是候选股证据（今日涨停，评估次日表现）：\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        + "\n请对每只输出严格 JSON 数组（不要多余文字）："
        '[{"code":"...","bull_case":"一句话做多逻辑","risk":"一句话主要风险",'
        '"watch_condition":"明日开盘/回踩的具体观察条件"}]。'
        "只使用给定证据，缺失就写“数据不足”。"
    )


def parse_llm_annotations(text: str) -> dict[str, dict[str, str]]:
    """Parse the model response into {code: {bull_case, risk, watch_condition}}.

    Tolerates markdown fences; unknown codes are dropped.
    """
    import json as _json

    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        arr = _json.loads(text)
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return {}
        try:
            arr = _json.loads(text[start:end + 1])
        except ValueError:
            return {}
    out: dict[str, dict[str, str]] = {}
    if isinstance(arr, list):
        for item in arr:
            if isinstance(item, dict) and item.get("code"):
                out[str(item["code"])] = {
                    "bull_case": str(item.get("bull_case") or "")[:200],
                    "risk": str(item.get("risk") or "")[:200],
                    "watch_condition": str(item.get("watch_condition") or "")[:200],
                }
    return out
