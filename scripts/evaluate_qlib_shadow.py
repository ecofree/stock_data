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
        f"- Evaluated samples: `{result.get('sample_count', 0)}`",
        "",
        "| Model | Samples | Hit Rate | Avg Return | Top Quantile | Bottom Quantile |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for model_id, item in sorted(result.get("models", {}).items()):
        lines.append(
            f"| `{model_id}` | {item['sample_count']} | {item['hit_rate']} | {item['avg_return']} | "
            f"{item['top_quantile_return']} | {item['bottom_quantile_return']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Evaluate qlib shadow predictions without affecting signals.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "qlib_shadow_latest.md"))
    args = parser.parse_args()

    ensure_qlib_shadow_tables(args.db)
    result = evaluate_qlib_shadow(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_qlib_shadow_report(result), encoding="utf-8")
    print(f"qlib_shadow_report={out}")
    print(f"qlib_shadow_samples={result['sample_count']}")
    print("signal_impact=disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
