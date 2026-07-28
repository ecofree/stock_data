from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.research.api_events import import_api_research_events


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Import compact KPL API event evidence into news_radar_item.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default="", help="Defaults to latest available API event date.")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--out", default=str(project_root / "reports" / "api_research_events_latest.md"))
    args = parser.parse_args()

    inserted = import_api_research_events(args.db, args.trade_date or None, args.limit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(
            [
                "# API Research Events",
                "",
                f"- Trade date: `{args.trade_date or 'latest_available'}`",
                f"- Imported items: `{inserted}`",
                "- Direct signal impact: `disabled`",
                "- Stored payload: compact metadata and hashes only; raw API responses remain in source tables.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"api_research_events_report={out}")
    print(f"api_research_events={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
