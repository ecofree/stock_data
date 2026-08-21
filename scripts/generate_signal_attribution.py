"""Aggregate stage-signal outcomes by market phase; write attribution table."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.logging_setup import configure  # noqa: E402
from trade_system.signal_attribution import (  # noqa: E402
    compute_stage_attribution,
    latest_phase,
)
from trade_system.cycle import PHASE_CN  # noqa: E402


def render_report(con, rows: list[dict], out_path: Path, phase_info: dict | None) -> None:
    lines = [
        "# Stage signal attribution",
        "",
        "Executable outcome = next-session open->close.  `cc` = close-to-close.",
        "",
    ]
    if phase_info:
        cap = phase_info.get("advisory_position_cap_pct")
        lines += [
            "## Latest phase (advisory)",
            "",
            f"- date: {phase_info['trade_date']}",
            f"- phase: {PHASE_CN.get(phase_info['phase'], phase_info['phase'])} "
            f"({phase_info['phase']}), score {phase_info['score']}",
            f"- rationale: {phase_info['rationale']}",
            f"- **advisory position cap: {cap if cap is not None else '—'}%**",
            "",
        ]
    lines += [
        "| stage | phase | n | win% | avg% | median% | horizon |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x["stage"], -(x["avg_ret_pct"] or 0))):
        win_txt = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
        lines.append(
            f"| {r['stage']} | {PHASE_CN.get(r['phase'], r['phase'])} | {r['n_signals']} "
            f"| {win_txt} "
            f"| {r['avg_ret_pct']} | {r['median_ret_pct']} | {r['horizon']} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports" / "signal_attribution_latest.md"))
    parser.add_argument("--include-not-triggered", action="store_true")
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db)
    try:
        rows = compute_stage_attribution(
            con, require_triggered=not args.include_not_triggered
        )
        phase_info = latest_phase(con)
        render_report(con, rows, Path(args.out), phase_info)
        print(f"attribution rows: {len(rows)}")
        print(f"report: {args.out}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
