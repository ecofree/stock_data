from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables
from trade_system.ml.shadow_evaluator import evaluate_qlib_shadow


def render_qlib_shadow_report(result: dict) -> str:
    lines = [
        "# Qlib Shadow Evaluation",
        "",
        "- Mode: `shadow`",
        "- Signal impact: `disabled`",
        f"- Evaluation: daily cross-sectional quantile; quantile=`{result.get('quantile', 0.2)}`; "
        f"round-trip cost=`{result.get('round_trip_cost_bps', 0.0)} bps`",
        f"- Evaluated samples: `{result.get('sample_count', 0)}`",
        "",
        "| Model | Samples | IC | RankIC | Hit Rate | Top Quantile | Bottom Quantile | Spread | Drawdown |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model_id, item in sorted(result.get("models", {}).items()):
        lines.append(
            f"| `{model_id}` | {item['sample_count']} | {item.get('ic')} | {item.get('rank_ic')} | "
            f"{item['hit_rate']} | {item['top_quantile_return']} | {item['bottom_quantile_return']} | "
            f"{item.get('top_bottom_spread')} | {item.get('max_drawdown')} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Evaluate qlib shadow predictions without affecting signals.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "qlib_shadow_latest.md"))
    parser.add_argument("--quantile", type=float, default=0.2)
    parser.add_argument("--round-trip-cost-bps", type=float, default=25.0)
    args = parser.parse_args()

    ensure_qlib_shadow_tables(args.db)
    result = evaluate_qlib_shadow(
        args.db,
        quantile=args.quantile,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    result["quantile"] = args.quantile
    result["round_trip_cost_bps"] = args.round_trip_cost_bps
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_qlib_shadow_report(result), encoding="utf-8")
    print(f"qlib_shadow_report={out}")
    print(f"qlib_shadow_samples={result['sample_count']}")
    print("signal_impact=disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
