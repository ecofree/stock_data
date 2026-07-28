from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.external_audit import audit_external_projects, write_phase11_outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 11 read-only audit for external integration projects.")
    parser.add_argument("--readonly", action="store_true", help="Document that this run is read-only.")
    parser.add_argument("--stock-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--legacy-root", default=r"D:\accio\A-share\kpl-qds")
    parser.add_argument("--tickflow-root", default=r"D:\accio\tickflow-stock-panel")
    parser.add_argument("--vibe-root", default=r"D:\accio\Vibe-Research")
    parser.add_argument("--out", default=r"D:\accio\stock_data\reports\external_project_audit_latest.md")
    parser.add_argument(
        "--matrix-out",
        default=r"D:\accio\stock_data\docs\integration\external_capability_matrix.md",
    )
    args = parser.parse_args()

    audit = audit_external_projects(
        stock_root=args.stock_root,
        legacy_root=args.legacy_root,
        tickflow_root=args.tickflow_root,
        vibe_root=args.vibe_root,
    )
    outputs = write_phase11_outputs(audit, report_path=args.out, matrix_path=args.matrix_out)

    print(f"external_project_audit={outputs['report']}")
    print(f"external_capability_matrix={outputs['matrix']}")
    print(f"capability_count={len(audit['capability_matrix'])}")
    print(f"forbidden_count={len(audit['forbidden_capabilities'])}")
    print(f"recommended_route={audit['recommended_route']}")
    return 0 if Path(args.stock_root).exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
