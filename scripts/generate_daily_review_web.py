"""Generate the detailed post-market review web page (HTML + ECharts).

Output: ``reports/daily_review_latest.html`` — a self-contained static page
built from the same context as the markdown daily review plus 30-session
trend series.  Open it in any browser; no server or CDN needed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DB_PATH  # noqa: E402
from trade_system.review_web import write_review_web  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the daily review web page.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--trade-date", default="",
                        help="Review date (YYYY-MM-DD); defaults to the latest stored session.")
    parser.add_argument("--as-of", default="", help="ISO timestamp for freshness gates.")
    parser.add_argument("--out", default=str(ROOT / "reports" / "daily_review_latest.html"))
    parser.add_argument(
        "--allow-direct-publish",
        action="store_true",
        help="Allow an explicit manual write to reports/*_latest.html; use only for controlled recovery.",
    )
    args = parser.parse_args()

    path = write_review_web(
        args.db,
        args.out,
        args.trade_date or None,
        allow_direct_publish=args.allow_direct_publish,
        as_of=args.as_of or None,
    )
    print(f"review_web={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
