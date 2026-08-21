from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.ai_review import build_ai_review_snapshot
from trade_system.deepseek_client import DeepSeekReviewClient, DeepSeekReviewError


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Generate a traceable AI-ready daily review facts snapshot.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date")
    parser.add_argument("--out", default=str(root / "reports" / "ai_review_facts_latest.json"))
    parser.add_argument("--call-model", action="store_true", help="Call configured DeepSeek provider after writing facts.")
    parser.add_argument("--ai-out", default=str(root / "reports" / "ai_review_latest.json"))
    parser.add_argument("--require-ai", action="store_true", help="Fail if the provider call or validation fails.")
    args = parser.parse_args()
    result = build_ai_review_snapshot(args.db, args.trade_date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"ai_review_snapshot={out}")
    print(f"trade_date={result.get('trade_date')} status={result['ai_contract']['status']} execution_ready={result.get('execution_ready')}")
    if args.call_model:
        ai_out = Path(args.ai_out)
        ai_out.parent.mkdir(parents=True, exist_ok=True)
        try:
            review = DeepSeekReviewClient().review(result)
            payload = {
                "schema_version": "ai_review_output_v1",
                "trade_date": result.get("trade_date"),
                "generated_at": result.get("generated_at"),
                "execution_ready": False,
                "analysis_only": True,
                **review,
            }
            ai_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"deepseek_status=success model={review.get('model')} attempts={review.get('attempts')} ai_out={ai_out}")
        except DeepSeekReviewError as exc:
            fallback = {
                "schema_version": "ai_review_output_v1",
                "trade_date": result.get("trade_date"),
                "generated_at": result.get("generated_at"),
                "execution_ready": False,
                "analysis_only": True,
                "status": "provider_unavailable",
                "error_type": type(exc).__name__,
                "error": str(exc)[:240],
                "review": None,
            }
            ai_out.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"deepseek_status=provider_unavailable ai_out={ai_out}")
            if args.require_ai:
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
