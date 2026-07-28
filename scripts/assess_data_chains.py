from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.data_chain import assess_data_chains, render_data_chain_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description="Assess professional trading data-chain coverage.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date")
    parser.add_argument("--out", default="reports/data_chain_status_latest.md")
    args = parser.parse_args()
    chains = assess_data_chains(args.db, args.date)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_data_chain_markdown(chains), encoding="utf-8")
    print("Data chain status:")
    for item in chains:
        print(f"{item['chain']}={item['status']}")
    print(f"out={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
