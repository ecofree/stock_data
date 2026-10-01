"""Audit candidate receipts and canonical label differences without certifying independence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trade_system.config import DB_PATH


def audit_candidate_file(path, expected_sha256, trade_date, securities, received_at, *,
                         provider='gangtise', request_url=None, source_document_paths=None):
    """Exact-byte offline receipt audit. It does not insert canonical facts."""
    import re
    from trade_system.flow_contract import parse_candidate_flow_response, parse_sina_flow_response
    if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('exact lowercase original response SHA256 required')
    with Path(path).open('rb') as source:
        raw = source.read(8_000_001)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('original candidate response SHA256 differs')
    if provider == 'sina':
        if (not isinstance(source_document_paths, dict) or set(source_document_paths) != {'page', 'fields', 'method'}
                or any(not isinstance(v, (str, Path)) for v in source_document_paths.values())):
            raise ValueError('all three Sina source document paths required')
        documents = {}
        for name, document in source_document_paths.items():
            with Path(document).open('rb') as source:
                documents[name] = source.read(1_000_001)
        return parse_sina_flow_response(raw, trade_date, securities, received_at=received_at,
                                        request_url=request_url, source_documents=documents)
    if provider != 'gangtise' or request_url is not None or source_document_paths is not None:
        raise ValueError('unknown candidate provider or incompatible source options')
    return parse_candidate_flow_response(raw, trade_date, securities, received_at=received_at)


def audit_product_selection_file(path, expected_sha256, trade_date, primary_origin, required_scope_sha256):
    """Screen an exact saved product observation; no requests or qualification writes."""
    import re
    from trade_system.flow_contract import select_flow_product_candidate
    if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('exact product selection evidence SHA256 required')
    with Path(path).open('rb') as source:
        raw = source.read(1_000_001)
    if not raw or len(raw) > 1_000_000 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('bounded original product selection SHA256 differs')
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate product selection field')
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError('nonfinite product selection field')
    candidate = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
    result = select_flow_product_candidate(candidate, trade_date, primary_origin=primary_origin,
                                           required_scope_sha256=required_scope_sha256)
    result['selection_evidence_sha256'] = expected_sha256
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--out", default="reports/stock_flow_contract_audit_latest.md")
    parser.add_argument("--date", default="")
    parser.add_argument('--candidate-plan', action='store_true', help='Build an offline Gangtise request plan; no requests.')
    parser.add_argument('--candidate-receipt', help='Audit original candidate JSON bytes offline; no database access.')
    parser.add_argument('--candidate-selection', help='Screen saved product evidence offline; no requests or database access.')
    parser.add_argument('--selection-sha256', help='Exact saved product observation SHA256.')
    parser.add_argument('--primary-origin', help='Explicit original compute source of the primary product.')
    parser.add_argument('--required-scope-sha256', help='Exact dated full required security scope fingerprint.')
    parser.add_argument('--candidate-provider', choices=('gangtise', 'sina'), default='gangtise')
    parser.add_argument('--request-url', help='Original Sina request URL, including its exact bounded query.')
    parser.add_argument('--sina-page-source', help='Saved official PC page bytes.')
    parser.add_argument('--sina-fields-source', help='Saved official PC field/formatter source bytes.')
    parser.add_argument('--sina-method-source', help='Saved official page-linked method document bytes.')
    parser.add_argument('--response-sha256', help='Exact original response SHA256.')
    parser.add_argument('--received-at', help='Original timezone-qualified receipt arrival, not audit time.')
    parser.add_argument('--codes', nargs='+', help='Explicit exchange-qualified expected securities.')
    args = parser.parse_args()
    sina_paths = {'page': args.sina_page_source, 'fields': args.sina_fields_source,
                  'method': args.sina_method_source}
    has_sina_options = args.request_url is not None or any(v is not None for v in sina_paths.values())
    if any(v is not None for v in (args.selection_sha256, args.primary_origin, args.required_scope_sha256)) and not args.candidate_selection:
        parser.error('product selection options require --candidate-selection')
    if (args.candidate_provider == 'sina' or has_sina_options) and not args.candidate_receipt:
        parser.error('Sina supports explicit offline --candidate-receipt only')
    if has_sina_options and args.candidate_provider != 'sina':
        parser.error('Sina source options require --candidate-provider sina')
    if args.candidate_plan or args.candidate_receipt or args.candidate_selection:
        if sum(bool(v) for v in (args.candidate_plan, args.candidate_receipt, args.candidate_selection)) != 1 or not args.date:
            parser.error('select one candidate mode with explicit --date')
        if not args.candidate_selection and not args.codes:
            parser.error('candidate plan or receipt requires explicit --codes')
        from trade_system.flow_contract import candidate_flow_capabilities, candidate_flow_request, GANGTISE_FLOW_URL
        try:
            if args.candidate_selection:
                result = audit_product_selection_file(args.candidate_selection, args.selection_sha256,
                    args.date, args.primary_origin, args.required_scope_sha256)
            elif args.candidate_receipt:
                if not args.received_at:
                    raise ValueError('original --received-at required')
                result = audit_candidate_file(args.candidate_receipt, args.response_sha256,
                                              args.date, args.codes, args.received_at,
                                              provider=args.candidate_provider,
                                              request_url=args.request_url,
                                              source_document_paths=(sina_paths if args.candidate_provider == 'sina' else None))
            else:
                result = dict(status='awaiting_entitlement_and_new_request_approval',
                              endpoint=GANGTISE_FLOW_URL,
                              request=candidate_flow_request(args.date, args.codes),
                              local_capabilities=candidate_flow_capabilities(),
                              max_requests_per_endpoint=2, max_requests_total=6,
                              timeout_seconds=20, automatic_retries=0, date_fallback=False,
                              authenticated_requests=0, production_writes=0,
                              independent_comparison_eligible=False)
            # Candidate evidence stays inside the unsealed checkout, independent
            # of --db or any live/runtime paths. Existing artifacts cannot be replaced.
            report = Path(args.out).resolve()
            if not report.is_relative_to(PROJECT_ROOT / 'reports'):
                raise ValueError('candidate output must be inside this checkout reports directory')
            report.parent.mkdir(parents=True, exist_ok=True)
            from trade_system.file_lock import FileLock
            with FileLock(str(report) + '.guard'):
                with report.open('x', encoding='utf-8') as output:
                    json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f'candidate_report={report} authenticated_requests=0 independent_eligible=False')
        return 0
    con = duckdb.connect(str(args.db), read_only=True)
    where = "WHERE NOT coalesce(is_stale,FALSE) AND main_net IS NOT NULL"
    params: list[str] = []
    if args.date:
        where += " AND source_date=CAST(? AS DATE)"
        params.append(args.date)
    summary = con.execute(
        f"""WITH x AS (
                SELECT source_date,stock_code,
                       string_agg(DISTINCT provider, ',' ORDER BY provider) providers,
                       string_agg(DISTINCT coalesce(flow_definition,'missing'), ',' ORDER BY coalesce(flow_definition,'missing')) definitions,
                       count(DISTINCT provider) provider_count,
                       count(DISTINCT coalesce(flow_definition,'missing')) definition_count,
                       min(main_net) min_main,max(main_net) max_main
                FROM multi_source_stock_flow {where}
                GROUP BY source_date,stock_code
                HAVING count(DISTINCT provider)>1
             )
             SELECT count(*),
                    count(*) FILTER (WHERE definition_count=1),
                    count(*) FILTER (WHERE definition_count>1),
                    count(*) FILTER (WHERE definition_count=1 AND abs(max_main-min_main)>greatest(10000.0,abs(max_main)*0.01)),
                    coalesce(max(abs(max_main-min_main)),0)
             FROM x""", params,
    ).fetchone()
    conflicts = con.execute(
        f"""WITH x AS (
                SELECT source_date,stock_code,
                       string_agg(DISTINCT provider, ',' ORDER BY provider) providers,
                       string_agg(DISTINCT coalesce(flow_definition,'missing'), ',' ORDER BY coalesce(flow_definition,'missing')) definitions,
                       min(main_net) min_main,max(main_net) max_main
                FROM multi_source_stock_flow {where}
                GROUP BY source_date,stock_code
                HAVING count(DISTINCT provider)>1 AND count(DISTINCT coalesce(flow_definition,'missing'))=1
             )
             SELECT source_date,stock_code,providers,definitions,min_main,max_main,abs(max_main-min_main) gap
             FROM x
             WHERE abs(max_main-min_main)>greatest(10000.0,abs(max_main)*0.01)
             ORDER BY gap DESC LIMIT 50""", params,
    ).fetchall()
    con.close()
    report = Path(args.out)
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Stock Flow Contract Audit",
        "",
        f"- Date filter: `{args.date or 'all'}`",
        "- Shared labels are diagnostic groups only. Independent comparability requires dated original-source evidence and matching definitions on all six axes.",
        "- This label audit does not certify independent reconciliation; use the existing independent comparison contract.",
        "",
        "| Overlap keys | Same-label keys | Cross-definition keys | Same-label differences | Max gap |",
        "|---:|---:|---:|---:|---:|",
        f"| {summary[0]} | {summary[1]} | {summary[2]} | {summary[3]} | {summary[4]} |",
        "",
        "## Same-label diagnostic differences",
        "",
        "| Date | Stock | Providers | Definition | Min Main | Max Main | Gap |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    lines.extend(f"| {date} | {code} | {providers} | {definitions} | {min_main} | {max_main} | {gap} |"
                 for date, code, providers, definitions, min_main, max_main, gap in conflicts)
    if not conflicts:
        lines.append("| none | | | | | | |")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report={report} overlap={summary[0]} same_label_differences={summary[3]} independent_certified=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
