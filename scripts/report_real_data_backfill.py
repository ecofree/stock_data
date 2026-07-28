from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.reports.real_data_backfill import (
    build_real_data_backfill_status,
    render_real_data_backfill_report,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Report real-data backfill progress and remaining gaps.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "real_data_backfill_latest.md"))
    args = parser.parse_args()

    status = build_real_data_backfill_status(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_real_data_backfill_report(status), encoding="utf-8")
    print(f"real_data_backfill_report={out}")
    for name, count in status["counts"].items():
        print(f"{name}={count}")
    print(f"gap_count={len(status['gaps'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
