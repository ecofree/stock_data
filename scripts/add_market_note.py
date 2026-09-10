"""Add (or replace) one market-level journal note for a trade date.

Example:
  python scripts/add_market_note.py --date 2026-08-21 \
      --note "高位分歧加剧，明日看修复还是退潮" --tags "分歧,高标风险"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--note", required=True)
    parser.add_argument("--tags", default="", help="逗号分隔标签")
    args = parser.parse_args()

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    try:
        con.execute(
            """INSERT INTO market_journal (trade_date, note, tags)
               VALUES (?, ?, ?)
               ON CONFLICT (trade_date) DO UPDATE SET
                 note=excluded.note, tags=excluded.tags,
                 created_at=now()""",
            [args.date, args.note.strip(), args.tags.strip()],
        )
        print(f"journal saved for {args.date}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
