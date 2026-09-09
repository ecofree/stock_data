from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.readiness import assess_trade_date_readiness, render_readiness_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description="Check same-date trading data readiness.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--trade-date", "--date", dest="trade_date", required=True)
    parser.add_argument(
        "--stage",
        choices=["premarket", "auction", "intraday", "close", "postmarket"],
        default="close",
    )
    parser.add_argument("--out", default="reports/data_readiness_latest.md")
    parser.add_argument("--report-only", action="store_true", help="Always exit zero after writing the report.")
    parser.add_argument(
        "--gate",
        choices=["source", "data", "flow", "analysis", "execution"],
        default="data",
        help="Gate represented by the exit code; default is the certified data gate.",
    )
    parser.add_argument(
        "--as-of",
        default="",
        help="Evaluate freshness at this ISO timestamp; used for audited historical recovery runs.",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=None,
        help="Reject same-date rows without a timestamp or older than this many seconds.",
    )
    args = parser.parse_args()

    result = assess_trade_date_readiness(
        args.db,
        args.trade_date,
        args.stage,
        max_age_seconds=args.max_age_seconds,
        now=datetime.fromisoformat(args.as_of) if args.as_of else None,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_readiness_markdown(result), encoding="utf-8")
    print(
        f"trade_date={result['trade_date']} stage={result['stage']} "
        f"source_ready={str(result.get('source_ready', result['ready'])).lower()} "
        f"pipeline_ready={str(result.get('pipeline_ready', False)).lower()} "
        f"data_certified_ready={str(result.get('data_certified_ready', False)).lower()} "
        f"flow_certified_ready={str(result.get('flow_certified_ready', 'not_assessed')).lower()} "
        f"analysis_ready={str(result.get('analysis_ready', False)).lower()} "
        f"execution_ready={str(result.get('execution_ready', False)).lower()} "
        f"actionable_candidates={result.get('actionable_candidates', 0)} "
        f"tradable_candidates={result.get('tradable_candidates', 0)} "
        f"risk_approved_candidates={result.get('risk_approved_candidates', 0)} "
        f"executable_candidates={result.get('executable_candidates', 0)} "
        f"missing={','.join(result['missing_groups']) or 'none'} out={out}"
    )
    gate_fields = {
        "source": "source_ready",
        "data": "data_certified_ready",
        "flow": "flow_certified_ready",
        "analysis": "analysis_ready",
        "execution": "execution_ready",
    }
    gate_ok = bool(result.get(gate_fields[args.gate], False))
    return 0 if gate_ok or args.report_only else 2


if __name__ == "__main__":
    raise SystemExit(main())
