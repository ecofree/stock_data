"""Persist an independent stock-flow reconciliation for a trade date.

The primary full-market flow is normally the delayed Eastmoney snapshot. A
different vendor label does not establish an independent same-definition
product: both sides need the reviewed source contract. This script compares canonical
rows already in DuckDB; it never refetches a provider or creates a new market
snapshot.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path
import math
import json
import hashlib
import sys

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


TABLE = "intraday_stock_flow_independent_reconciliation"
RULE_VERSION = 'independent-flow-v2-amount-and-dated-scope'


def _ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            trade_date DATE PRIMARY KEY,
            primary_provider VARCHAR,
            reference_provider VARCHAR,
            primary_rows INTEGER DEFAULT 0,
            reference_rows INTEGER DEFAULT 0,
            overlap_rows INTEGER DEFAULT 0,
            primary_only_rows INTEGER DEFAULT 0,
            reference_only_rows INTEGER DEFAULT 0,
            overlap_reference_pct DOUBLE DEFAULT 0,
            correlation_main_net DOUBLE,
            sign_agreement_pct DOUBLE,
            mean_abs_diff_pct DOUBLE,
            primary_net_total DOUBLE,
            reference_net_total DOUBLE,
            status VARCHAR,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(f'ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS evidence_json VARCHAR')
    con.execute(f'ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS rule_version VARCHAR')


def _rows(con: duckdb.DuckDBPyConnection, trade_date: str, provider: str) -> dict[str, float]:
    rows = con.execute(
        """
        SELECT stock_code, main_net
        FROM multi_source_stock_flow
        WHERE source_date=CAST(? AS DATE) AND provider=? AND is_stale=FALSE
          AND stock_code IS NOT NULL AND main_net IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY stock_code ORDER BY collected_at DESC NULLS LAST, fetched_at DESC NULLS LAST)=1
        """,
        [trade_date, provider],
    ).fetchall()
    return {str(code): float(value) for code, value in rows if code and value is not None and math.isfinite(value)}


def reconcile(
    db_path: str | Path,
    trade_date: str,
    *,
    primary_provider: str | None = None,
    reference_provider: str = "tushare",
) -> dict:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        _ensure_table(con)
        if primary_provider is None:
            from trade_system.quality import table_exists
            batch = con.execute('SELECT provider FROM intraday_stock_flow_batch WHERE trade_date=?',
                [trade_date]).fetchone() if table_exists(con,'intraday_stock_flow_batch') else None
            primary_provider = batch[0] if batch else 'eastmoney_intraday_clist_delay'
        primary = _rows(con, trade_date, primary_provider)
        reference = _rows(con, trade_date, reference_provider)
        from scripts.collect_intraday_stock_flow_market import _a_share_universe_by_exchange
        universe = set(_a_share_universe_by_exchange(con, trade_date))
        overlap_codes = sorted(set(primary) & set(reference) & universe)
        primary_only = set(primary) - set(reference)
        reference_only = set(reference) - set(primary)
        pairs = [(primary[c], reference[c]) for c in overlap_codes]
        n = len(pairs)
        correlation = None
        sign_agreement = None
        mean_abs_diff = None
        if n:
            p_mean = sum(p for p, _ in pairs) / n
            r_mean = sum(r for _, r in pairs) / n
            p_var = sum((p - p_mean) ** 2 for p, _ in pairs)
            r_var = sum((r - r_mean) ** 2 for _, r in pairs)
            if p_var > 0 and r_var > 0:
                correlation = sum((p - p_mean) * (r - r_mean) for p, r in pairs) / math.sqrt(p_var * r_var)
            sign_agreement = sum(1 for p, r in pairs if (p > 0)-(p < 0) == (r > 0)-(r < 0)) * 100.0 / n
            mean_abs_diff = sum(abs(p - r) / max(abs(p), abs(r), 1_000_000.0) for p, r in pairs) * 100.0 / n
        overlap_pct = len(overlap_codes) * 100.0 / len(universe) if universe else 0.0
        from trade_system.flow_contract import independent_comparison_contract, stock_flow_evidence_fingerprint
        comparison = independent_comparison_contract(con, trade_date, primary_provider, reference_provider)
        policy = comparison.get('definition_evidence', {}).get('amount_precision', {})
        quantums = [policy.get(k) for k in ('primary_quantum_yuan', 'reference_quantum_yuan')]
        precision_valid = all(isinstance(v, (int,float)) and not isinstance(v,bool)
                              and math.isfinite(v) and v > 0 for v in quantums)
        absolute_tolerance = sum(quantums)/2 if precision_valid else None
        disagreements = [c for c in overlap_codes if absolute_tolerance is None or
            abs(primary[c]-reference[c]) > absolute_tolerance + 1e-9*max(abs(primary[c]),abs(reference[c]))]
        amount_match_pct = (n-len(disagreements))*100.0/n if n else 0.0
        clocks = {}
        close_at = datetime.combine(date.fromisoformat(trade_date), time(15))
        for provider in (primary_provider, reference_provider):
            minimum, maximum, absent = con.execute('''SELECT min(fetched_at),max(fetched_at),
                count(*) FILTER (WHERE fetched_at IS NULL) FROM multi_source_stock_flow
                WHERE source_date=? AND provider=? AND is_stale=FALSE''', [trade_date,provider]).fetchone()
            clocks[provider] = {'min':str(minimum), 'max':str(maximum), 'missing':absent}
            if absent or minimum is None or minimum < close_at or maximum > datetime.now():
                comparison.update(eligible=False, reason='close_input_time_unqualified')
        status = 'warning'
        if not comparison['eligible']:
            status = 'incomparable'
        elif not universe:
            status = 'unknown_scope'
        elif not precision_valid:
            status = 'unverified_amount_precision'
        elif (overlap_pct >= 99.5 and amount_match_pct >= 99.5
              and not (set(primary) | set(reference))-universe):
            status = 'pass'
        result = {
            'rule_version':RULE_VERSION,
            'expected_codes_sha256':hashlib.sha256(json.dumps(sorted(universe)).encode()).hexdigest(),
            'source_fingerprints':{p:stock_flow_evidence_fingerprint(con,trade_date,p)
                                   for p in (primary_provider,reference_provider)},
            'comparison_contract': comparison,
            'expected_rows':len(universe), 'coverage_denominator':'dated_approved_stock_universe',
            'input_arrival_times':clocks, 'absolute_tolerance_yuan':absolute_tolerance,
            'amount_match_pct':amount_match_pct, 'amount_mismatch_codes':disagreements,
            'unexpected_codes':sorted((set(primary)|set(reference))-universe),
            "scope": "declared_data_origin_comparison_not_independent_transport_attestation",
            "trade_date": trade_date,
            "primary_provider": primary_provider,
            "reference_provider": reference_provider,
            "primary_rows": len(primary),
            "reference_rows": len(reference),
            "overlap_rows": len(overlap_codes),
            "primary_only_rows": len(primary_only),
            "reference_only_rows": len(reference_only),
            "overlap_reference_pct": round(overlap_pct, 4),
            "correlation_main_net": round(correlation, 6) if correlation is not None else None,
            "sign_agreement_pct": round(sign_agreement, 4) if sign_agreement is not None else None,
            "mean_abs_diff_pct": round(mean_abs_diff, 4) if mean_abs_diff is not None else None,
            "primary_net_total": round(sum(primary.values()), 4),
            "reference_net_total": round(sum(reference.values()), 4),
            "status": status,
            "last_error": ("" if status=='pass' else comparison['reason'] if not comparison['eligible']
                           else status),
        }
        con.execute(
            f"""
            INSERT INTO {TABLE} (
                trade_date,primary_provider,reference_provider,primary_rows,reference_rows,
                overlap_rows,primary_only_rows,reference_only_rows,overlap_reference_pct,
                correlation_main_net,sign_agreement_pct,mean_abs_diff_pct,
                primary_net_total,reference_net_total,status,last_error,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_date) DO UPDATE SET
                primary_provider=excluded.primary_provider,
                reference_provider=excluded.reference_provider,
                primary_rows=excluded.primary_rows,
                reference_rows=excluded.reference_rows,
                overlap_rows=excluded.overlap_rows,
                primary_only_rows=excluded.primary_only_rows,
                reference_only_rows=excluded.reference_only_rows,
                overlap_reference_pct=excluded.overlap_reference_pct,
                correlation_main_net=excluded.correlation_main_net,
                sign_agreement_pct=excluded.sign_agreement_pct,
                mean_abs_diff_pct=excluded.mean_abs_diff_pct,
                primary_net_total=excluded.primary_net_total,
                reference_net_total=excluded.reference_net_total,
                status=excluded.status,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            [
                result["trade_date"], result["primary_provider"], result["reference_provider"],
                result["primary_rows"], result["reference_rows"], result["overlap_rows"],
                result["primary_only_rows"], result["reference_only_rows"], result["overlap_reference_pct"],
                result["correlation_main_net"], result["sign_agreement_pct"], result["mean_abs_diff_pct"],
                result["primary_net_total"], result["reference_net_total"], result["status"], result["last_error"],
                datetime.now(),
            ],
        )
        con.execute(f'UPDATE {TABLE} SET evidence_json=?,rule_version=? WHERE trade_date=?',
                    [json.dumps(result,default=str), RULE_VERSION,trade_date])
        con.commit()
        return result
    finally:
        con.close()


def render_report(results: list[dict]) -> str:
    lines = [
        "# Independent Stock Flow Reconciliation",
        "",
        "Recorded primary and reference products are compared under their reviewed same-definition contract. Vendor labels, auxiliary research and correlated values do not certify independence.",
        "",
        "| Trade date | Primary rows | Reference rows | Overlap | Coverage | Corr(main_net) | Sign agreement | Mean abs diff | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['trade_date']} | {r['primary_rows']} | {r['reference_rows']} | {r['overlap_rows']} | "
            f"{r['overlap_reference_pct']:.2f}% | {r['correlation_main_net'] if r['correlation_main_net'] is not None else ''} | "
            f"{r['sign_agreement_pct'] if r['sign_agreement_pct'] is not None else ''}% | "
            f"{r['mean_abs_diff_pct'] if r['mean_abs_diff_pct'] is not None else ''}% | {r['status']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist independent stock-flow reconciliation metrics.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", dest="trade_date", action="append", help="Trade date; repeat for multiple dates.")
    parser.add_argument("--out", default="reports/independent_stock_flow_reconciliation_latest.md")
    args = parser.parse_args()
    dates = args.trade_date or [date.today().isoformat()]
    results = [reconcile(args.db, d) for d in dates]
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(results), encoding="utf-8")
    for r in results:
        print(
            f"date={r['trade_date']} status={r['status']} primary={r['primary_rows']} "
            f"tushare={r['reference_rows']} overlap={r['overlap_reference_pct']:.2f}% "
            f"corr={r['correlation_main_net']} sign={r['sign_agreement_pct']}%"
        )
    print(f"out={path}")
    return 0 if all(r["status"] == "pass" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
