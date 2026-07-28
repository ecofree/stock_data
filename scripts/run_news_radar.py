from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.adapters.vibe_research import discover_vibe_news_sources, load_vibe_news_config, radar_skeleton_from_config
from trade_system.research.news_radar import normalize_radar_items, upsert_news_items
from trade_system.research.schema import ensure_research_tables


def render_news_report(radar: dict, inserted: int, mode: str) -> str:
    stats = radar.get("stats", {})
    lines = [
        "# News Radar Import",
        "",
        f"- Mode: `{mode}`",
        f"- Generated at: `{radar.get('generated_at')}`",
        f"- Industries: `{stats.get('industries', len(radar.get('industries', [])))}`",
        f"- Sources: `{stats.get('total_sources', 0)}`",
        f"- Inserted items: `{inserted}`",
        "",
        "| Industry | Source count | Item count |",
        "|---|---:|---:|",
    ]
    for industry in radar.get("industries", []):
        lines.append(
            f"| {industry.get('name', industry.get('key', ''))} | "
            f"{industry.get('total', 0)} | {len(industry.get('items', []))} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Import Vibe-Research news radar data into stock_data research tables.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--vibe-root", default=r"D:\accio\Vibe-Research")
    parser.add_argument("--source-json", default="")
    parser.add_argument("--radar-json", default="", help="Optional existing Vibe radar cache JSON with items.")
    parser.add_argument("--out", default=str(project_root / "reports" / "news_radar_latest.md"))
    args = parser.parse_args()

    ensure_research_tables(args.db)
    if args.radar_json:
        radar = json.loads(Path(args.radar_json).read_text(encoding="utf-8"))
        mode = "radar_json"
    else:
        source_json = discover_vibe_news_sources(
            explicit_path=Path(args.source_json) if args.source_json else None,
            vibe_root=args.vibe_root,
        )
        if source_json:
            config = load_vibe_news_config(source_json)
            radar = radar_skeleton_from_config(config)
            mode = "skeleton"
        else:
            radar = {"generated_at": None, "recent_days": 0, "industries": [], "stats": {"industries": 0, "total_sources": 0}}
            mode = "missing_config"

    rows = normalize_radar_items(radar, trade_date=args.trade_date)
    inserted = upsert_news_items(args.db, rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_news_report(radar, inserted, mode), encoding="utf-8")
    print(f"news_radar_report={out}")
    print(f"news_items={inserted}")
    print(f"mode={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
