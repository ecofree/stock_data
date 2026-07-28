from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.retirement import (
    build_retirement_checklist,
    persist_retirement_checklist,
    render_retirement_checklist_markdown,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build Phase 18 external project retirement checklist.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--stock-root", default=str(project_root))
    parser.add_argument("--legacy-root", default=r"D:\accio\A-share\kpl-qds")
    parser.add_argument("--tickflow-root", default=r"D:\accio\tickflow-stock-panel")
    parser.add_argument("--vibe-root", default=r"D:\accio\Vibe-Research")
    parser.add_argument("--doc", default=str(project_root / "docs" / "integration" / "retirement_checklist.md"))
    parser.add_argument("--out", default=str(project_root / "reports" / "retirement_readiness_latest.md"))
    args = parser.parse_args()

    rows = build_retirement_checklist(args.stock_root, args.legacy_root, args.tickflow_root, args.vibe_root)
    persist_retirement_checklist(args.db, rows)
    markdown = render_retirement_checklist_markdown(rows)
    for target in [Path(args.doc), Path(args.out)]:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
    print(f"retirement_checklist={args.doc}")
    print(f"retirement_report={args.out}")
    print(f"rows={len(rows)}")
    print(f"delete_safe_count={sum(1 for row in rows if row['delete_safe'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
