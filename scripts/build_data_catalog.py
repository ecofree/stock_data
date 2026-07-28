from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.data_catalog import (
    build_default_data_sources,
    install_data_catalog,
    render_adapter_contract_markdown,
    render_data_source_catalog_markdown,
)
from trade_system.integration.external_audit import audit_external_projects


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build Phase 12 unified data catalog and capability registry.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--stock-root", default=str(project_root))
    parser.add_argument("--legacy-root", default=r"D:\accio\A-share\kpl-qds")
    parser.add_argument("--tickflow-root", default=r"D:\accio\tickflow-stock-panel")
    parser.add_argument("--vibe-root", default=r"D:\accio\Vibe-Research")
    parser.add_argument("--catalog-doc", default=str(project_root / "docs" / "integration" / "data_source_catalog.md"))
    parser.add_argument("--contract-doc", default=str(project_root / "docs" / "integration" / "adapter_contract.md"))
    args = parser.parse_args()

    audit = audit_external_projects(args.stock_root, args.legacy_root, args.tickflow_root, args.vibe_root)
    counts = install_data_catalog(args.db, audit)

    catalog_doc = Path(args.catalog_doc)
    contract_doc = Path(args.contract_doc)
    catalog_doc.parent.mkdir(parents=True, exist_ok=True)
    contract_doc.parent.mkdir(parents=True, exist_ok=True)
    catalog_doc.write_text(render_data_source_catalog_markdown(build_default_data_sources()), encoding="utf-8")
    contract_doc.write_text(render_adapter_contract_markdown(), encoding="utf-8")

    print(f"data_source_catalog={counts['data_source_catalog']}")
    print(f"external_capability_registry={counts['external_capability_registry']}")
    print(f"catalog_doc={catalog_doc}")
    print(f"contract_doc={contract_doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
