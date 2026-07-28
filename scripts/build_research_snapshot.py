from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.research.schema import ensure_research_tables


def build_snapshot(db_path: str | Path) -> dict:
    ensure_research_tables(db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {
            "news_radar_item": con.execute("SELECT count(*) FROM news_radar_item").fetchone()[0],
            "research_note": con.execute("SELECT count(*) FROM research_note").fetchone()[0],
            "research_report_file": con.execute("SELECT count(*) FROM research_report_file").fetchone()[0],
            "v_operator_research_context": con.execute("SELECT count(*) FROM v_operator_research_context").fetchone()[0],
        }
        latest_news = con.execute(
            "SELECT trade_date, related_sector, title FROM news_radar_item ORDER BY trade_date DESC, title LIMIT 10"
        ).fetchall()
        return {"counts": counts, "latest_news": latest_news}
    finally:
        con.close()


def render_snapshot(snapshot: dict) -> str:
    lines = ["# Research Snapshot", "", "| Object | Rows |", "|---|---:|"]
    for name, count in snapshot["counts"].items():
        lines.append(f"| `{name}` | {count} |")
    lines.extend(["", "## Latest News", "", "| Date | Sector | Title |", "|---|---|---|"])
    for trade_date, sector, title in snapshot["latest_news"]:
        lines.append(f"| {trade_date} | {sector or ''} | {title} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build a research-layer snapshot report.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "research_snapshot_latest.md"))
    args = parser.parse_args()

    snapshot = build_snapshot(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_snapshot(snapshot), encoding="utf-8")
    print(f"research_snapshot={out}")
    for name, count in snapshot["counts"].items():
        print(f"{name}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
