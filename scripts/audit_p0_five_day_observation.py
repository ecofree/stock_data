from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.p0_observation import (  # noqa: E402
    audit_five_day_observation,
    render_observation,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the rolling strict P0 observation window.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--required-days", type=int, default=5)
    parser.add_argument("--minimum-ths-concepts", type=int, default=374)
    parser.add_argument("--out", default="reports/p0_five_day_observation_latest.md")
    args = parser.parse_args()

    result = audit_five_day_observation(
        args.db,
        args.reports_dir,
        args.as_of,
        required_days=args.required_days,
        minimum_ths_concepts=args.minimum_ths_concepts,
    )
    content = render_observation(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    dated = out.parent / f"p0_five_day_observation_{args.as_of}.md"
    dated.write_text(content, encoding="utf-8")
    print(
        f"P0_OBSERVATION as_of={args.as_of} "
        f"consecutive={result['consecutive_passes']}/{result['required_days']} "
        f"ready_for_p1={str(result['ready_for_p1']).lower()} out={out}"
    )
    # This is an observer, not a data collector.  A red report must be
    # persisted without turning the close task itself into a new failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
