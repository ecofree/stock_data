"""Print the phase/source matrix and optional freshness decisions."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.collection_profiles import PHASES, phase_tasks, render_matrix, task_due


def main() -> int:
    parser = argparse.ArgumentParser(description="Describe auction/intraday/close/history collection profiles.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--phase", choices=PHASES, default="")
    parser.add_argument("--out", default="reports/collection_phase_matrix_latest.md")
    args = parser.parse_args()
    text = render_matrix()
    if args.phase:
        lines = [text.rstrip(), "", f"## Current freshness ({args.phase}, {args.date})", "", "| task | due | reason |", "|---|---|---|"]
        for task in phase_tasks(args.phase):
            due, reason = task_due(args.db, args.date, task.name)
            lines.append(f"| {task.name} | {'yes' if due else 'no'} | {reason} |")
        text = "\n".join(lines) + "\n"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"phase_matrix={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

