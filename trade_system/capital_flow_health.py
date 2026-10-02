"""Focused health checks for stock and sector capital-flow evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from trade_system.db_utils import legacy_connect as connect_duckdb
from trade_system.gate_contract import build_operator_state, build_operational_capabilities
from trade_system.quality import table_columns, table_exists
from trade_system.time_utils import as_local_naive
from trade_system.readiness import capital_flow_coverage, qualified_stock_flow_scope


STOCK_FLOW_RELATIONS = (
    "multi_source_stock_flow",
    "l2_stock_intraday",
    "l2_stock_bigorder",
    "advanced_zjmm_min",
    "advanced_dadan_kline",
    "advanced_main_activity_kline",
    "advanced_pankou",
)

SECTOR_FLOW_RELATIONS = (
    "multi_source_sector_flow",
    "sector_capital",
    "l2_sector_intraday",
    "l2_sector_volume",
)


def _relation_health(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    trade_date: str,
    code_column: str,
    collected_after: datetime | None = None,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
    provider: str | None = None,
) -> dict:
    collected_after = as_local_naive(collected_after)
    now = as_local_naive(now) or datetime.now()
    if not table_exists(con, relation):
        return {
            "relation": relation,
            "rows": 0,
            "codes": 0,
            "latest_timestamp": None,
            "latest_source_time": None,
            "recent_rows": 0,
            "recent_codes": 0,
            "status": "missing_relation",
        }
    columns = set(table_columns(con, relation))
    date_column = next((name for name in ("trade_date", "date", "source_date") if name in columns), None)
    if not date_column:
        return {
            "relation": relation,
            "rows": 0,
            "codes": 0,
            "latest_timestamp": None,
            "latest_source_time": None,
            "recent_rows": 0,
            "recent_codes": 0,
            "status": "missing_date_column",
        }
    timestamp_column = next(
        (name for name in ("fetched_at", "updated_at", "generated_at") if name in columns),
        None,
    )
    code_expr = f'count(distinct "{code_column}")' if code_column in columns else "0"
    timestamp_expr = f'max("{timestamp_column}")' if timestamp_column else "NULL"
    source_time_expr = 'max(CAST("time" AS VARCHAR))' if "time" in columns else "NULL"
    # A legacy sector row can contain only a date/code after an upstream
    # quote-only response.  Count a sector row as usable only when at least
    # one directional flow bucket is present.  Test fixtures that intentionally
    # model a minimal relation without flow columns retain the old semantics.
    flow_columns = (
        ("main_net", "super_net", "large_net", "mid_net", "small_net")
        if relation == "multi_source_sector_flow"
        else ("main_net_inflow", "super_net_inflow", "big_net_inflow", "mid_net_inflow", "small_net_inflow")
        if relation == "sector_capital"
        else ()
    )
    valid_flow = (
        "(" + " OR ".join(f'isfinite(TRY_CAST("{name}" AS DOUBLE))' for name in flow_columns if name in columns) + ")"
        if any(name in columns for name in flow_columns)
        else "TRUE"
    )
    if relation == "multi_source_stock_flow" and "main_net" in columns:
        valid_flow = 'isfinite(TRY_CAST("main_net" AS DOUBLE))'
    date_filter = f'"{date_column}" = CAST(? AS DATE) AND {valid_flow}'
    if 'is_stale' in columns:
        date_filter += ' AND coalesce(is_stale, FALSE)=FALSE'
    date_params: list[object] = [trade_date]
    if provider is not None:
        if 'provider' in columns:
            date_filter += ' AND provider=?'
            date_params.append(provider)
        else:
            date_filter += ' AND FALSE'
    if now is not None and timestamp_column:
        date_filter += f' AND "{timestamp_column}" <= ?'
        date_params.append(now)
    rows, codes, latest, latest_source_time = con.execute(
        f"""
        SELECT count(*), {code_expr}, {timestamp_expr}, {source_time_expr}
        FROM "{relation}"
        WHERE {date_filter}
        """,
        date_params,
    ).fetchone()
    recent_rows = int(rows or 0)
    recent_codes = int(codes or 0)
    if collected_after is not None or max_age_seconds is not None:
        if timestamp_column:
            freshness_filters = []
            freshness_params = list(date_params)
            if collected_after is not None:
                freshness_filters.append(f'"{timestamp_column}" >= ?')
                freshness_params.append(collected_after)
            if max_age_seconds is not None:
                cutoff = (now or datetime.now()) - timedelta(seconds=max(0, int(max_age_seconds)))
                freshness_filters.append(f'"{timestamp_column}" >= ?')
                freshness_params.append(cutoff)
            recent_rows, recent_codes = con.execute(
                f"""
                SELECT count(*), {code_expr}
                FROM "{relation}"
                WHERE {date_filter}
                  AND {' AND '.join(freshness_filters)}
                """,
                freshness_params,
            ).fetchone()
            recent_rows = int(recent_rows or 0)
            recent_codes = int(recent_codes or 0)
        else:
            recent_rows = 0
            recent_codes = 0
    effective_rows = recent_rows if (collected_after is not None or max_age_seconds is not None) else int(rows or 0)
    status = "ready" if effective_rows else "old_for_date" if rows else "empty_for_date"
    freshness_age_seconds = None
    if latest is not None:
        try:
            observed_at = as_local_naive(latest)
            freshness_age_seconds = max(0.0, ((now or datetime.now()) - observed_at).total_seconds())
        except (TypeError, ValueError):
            freshness_age_seconds = None
    if max_age_seconds is not None:
        if not timestamp_column or latest is None:
            effective_rows = 0
            recent_rows, recent_codes = 0, 0
            status = "missing_timestamp"
        elif freshness_age_seconds is None or freshness_age_seconds > max(0, int(max_age_seconds)):
            effective_rows = 0
            recent_rows, recent_codes = 0, 0
            status = "stale_timestamp"
        elif effective_rows < int(rows or 0):
            status = "fresh_partial"
        else:
            status = "ready"
    return {
        "relation": relation,
        "rows": int(rows or 0),
        "codes": int(codes or 0),
        "latest_timestamp": str(latest) if latest is not None else None,
        "latest_source_time": str(latest_source_time) if latest_source_time is not None else None,
        "freshness_age_seconds": freshness_age_seconds,
        "max_age_seconds": int(max_age_seconds) if max_age_seconds is not None else None,
        "recent_rows": recent_rows,
        "recent_codes": recent_codes,
        "status": status,
    }


def assess_capital_flow_health(
    db_path: str | Path,
    trade_date: str,
    expected_stock_codes: int = 0,
    expected_sector_codes: int = 0,
    collected_after: str | datetime | None = None,
    min_coverage_pct: float = 99.5,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
    session_close: bool = False,
) -> dict:
    if isinstance(collected_after, str):
        collected_after = as_local_naive(collected_after)
    else:
        collected_after = as_local_naive(collected_after)
    now = as_local_naive(now) or datetime.now()
    # Never silently turn off an explicitly requested TTL for an older date.
    # Historical as-of replay supplies its historical clock; retrospective
    # date-only inspection may explicitly omit TTL, without certifying freshness.
    effective_max_age_seconds = max_age_seconds
    close_boundary = datetime.fromisoformat(trade_date + "T15:00:00")
    if session_close and now >= close_boundary:
        # Retained same-session close evidence does not expire during retry.
        # Earlier intraday rows and future fetches remain excluded; coverage,
        # taxonomy and independent reconciliation gates still apply.
        effective_max_age_seconds = int((now - close_boundary).total_seconds())
    con = connect_duckdb(str(db_path), read_only=True)
    primary_stock_provider = None
    primary_stock_codes = None
    try:
        stock_qualification = capital_flow_coverage(con, trade_date, "stock", require_actual=True,
            max_age_seconds=effective_max_age_seconds, now=now, collected_after=collected_after)
        sector_qualification = capital_flow_coverage(con, trade_date, "sector", require_actual=True,
            max_age_seconds=effective_max_age_seconds, now=now, collected_after=collected_after)
        # The shared gate preserves unknown/ambiguous metadata; do not pick
        # an arbitrary duplicate batch or cast its NaN denominator to int.
        if not expected_stock_codes:
            expected_stock_codes = stock_qualification.get("expected_rows") or 0
        primary_stock_provider = stock_qualification.get("provider")
        if primary_stock_provider and table_exists(con, "multi_source_stock_flow"):
            primary_health = _relation_health(con, 'multi_source_stock_flow', trade_date,
                'stock_code', collected_after=collected_after,
                max_age_seconds=effective_max_age_seconds, now=now, provider=primary_stock_provider)
            primary_stock_codes = primary_health['recent_codes']
        if not expected_sector_codes:
            expected_sector_codes = sector_qualification.get("expected_rows") or 0
        stock_relations = [
            _relation_health(
                con, relation, trade_date, "stock_code", collected_after,
                max_age_seconds=effective_max_age_seconds, now=now,
            )
            for relation in STOCK_FLOW_RELATIONS
        ]
        sector_relations = [
            _relation_health(
                con, relation, trade_date, "sector_code", collected_after,
                max_age_seconds=effective_max_age_seconds, now=now,
            )
            for relation in SECTOR_FLOW_RELATIONS
        ]
    finally:
        con.close()

    gated = collected_after is not None or effective_max_age_seconds is not None
    code_field = "recent_codes" if gated else "codes"
    # Coverage is measured against the batch's own provider/universe.  Taking
    # the union across TuShare and Eastmoney produced impossible ratios such
    # as 5,547 / 5,539 and hid the 17 unavailable primary rows.
    stock_codes = (
        int(primary_stock_codes)
        if primary_stock_codes is not None
        else max((item[code_field] for item in stock_relations
                  if item["relation"] == "multi_source_stock_flow"), default=0)
    )
    stock_coverage = (
        stock_codes * 100.0 / expected_stock_codes if expected_stock_codes else None
    )
    stock_ready = bool(stock_qualification["passed"] and expected_stock_codes == stock_qualification["expected_rows"]
                       and stock_qualification.get("actual", {}).get("coverage_pct", 0) >= max(99.5, min_coverage_pct))
    # Sector capital is the core directional flow; intraday sector volume alone is not equivalent.
    sector_candidates = [
        item for item in sector_relations
        if item["relation"] in {"multi_source_sector_flow", "sector_capital"}
    ]
    sector_codes = max((item[code_field] for item in sector_candidates), default=0)
    sector_coverage = (
        sector_codes * 100.0 / expected_sector_codes if expected_sector_codes else None
    )
    sector_ready = bool(sector_qualification["passed"] and expected_sector_codes == sector_qualification["expected_rows"]
                        and all(fact.get("coverage_pct", 0) >= max(99.5, min_coverage_pct)
                                for fact in sector_qualification.get("actual", {}).get("taxonomies", {}).values()))
    # A4: surface stale THS concept membership.  The concept taxonomy can be
    # coverage-complete yet built on an out-of-date membership snapshot; that must
    # be visible to the gate/report even though directional industry flow is fine.
    sector_taxonomy_stale = False
    sector_taxonomy_note = ""
    canonical_membership_snapshot = None
    derived_membership_snapshots: list[str] = []
    membership_batch_consistent = True
    try:
        con = connect_duckdb(str(db_path), read_only=True)
        if table_exists(con, "intraday_sector_flow_taxonomy"):
            stale_row = con.execute(
                "SELECT status, last_error FROM intraday_sector_flow_taxonomy "
                "WHERE trade_date=CAST(? AS DATE) AND taxonomy='ths_concept'",
                [trade_date],
            ).fetchone()
            if stale_row and str(stale_row[0] or "").lower() in {"stale_members", "partial_members"}:
                sector_taxonomy_stale = True
                sector_taxonomy_note = str(stale_row[1] or "ths concept membership is stale or partial")
        from trade_system.ths_quality import canonical_ths_snapshot
        snapshot = canonical_ths_snapshot(con, trade_date)
        canonical_membership_snapshot = snapshot['snapshot_date'] if snapshot else None
        if table_exists(con, "multi_source_sector_flow"):
            sector_columns = set(table_columns(con, "multi_source_sector_flow"))
            derived_membership_snapshots = [
                str(row[0])
                for row in con.execute(
                    """
                    SELECT DISTINCT json_extract_string(
                        raw_json, '$.raw.membership_snapshot_date'
                    ) AS snapshot_date
                    FROM multi_source_sector_flow
                    WHERE source_date=CAST(? AS DATE)
                      AND provider='derived_ths_stock_aggregate'
                      AND coalesce(is_stale,FALSE)=FALSE
                    ORDER BY snapshot_date
                    """,
                    [trade_date],
                ).fetchall()
                if row[0]
            ] if {'provider','raw_json','is_stale'} <= sector_columns else []
        if derived_membership_snapshots:
            membership_batch_consistent = (
                canonical_membership_snapshot is not None
                and derived_membership_snapshots == [canonical_membership_snapshot]
            )
            if not membership_batch_consistent:
                sector_taxonomy_stale = True
                sector_taxonomy_note = (
                    "derived THS sector flow membership snapshots "
                    f"{derived_membership_snapshots} do not match canonical "
                    f"{canonical_membership_snapshot or 'none'}"
                )
        con.close()
    except Exception as exc:
        sector_taxonomy_stale = True
        sector_taxonomy_note = "taxonomy verification failed: " + type(exc).__name__
    # P1-2: surface independent-source reconciliation.  Coverage alone can pass while
    # accuracy rests on a single (delayed) Eastmoney source; the reconciliation status
    # and whether an independent provider (TuShare moneyflow) exists for the date must
    # be visible rather than silently passing.
    recon_status = "not_run"
    recon_reference_rows = 0
    recon_value_status = "not_observed"
    recon_sign_disagreement_pct = None
    recon_mean_abs_main_net_diff = None
    independent_source_present = False
    independent_reference_provider = None
    independent_reference_qualification = {}
    independent_status = "not_run"
    independent_overlap_pct = None
    independent_correlation = None
    independent_sign_agreement_pct = None
    comparison_contract = {'eligible': False, 'reason': 'not_observed'}
    try:
        con = connect_duckdb(str(db_path), read_only=True)
        if table_exists(con, "intraday_stock_flow_reconciliation"):
            columns = set(table_columns(con, "intraday_stock_flow_reconciliation"))
            select_columns = ["status", "reference_rows"]
            if "value_status" in columns:
                select_columns.append("value_status")
            if "overlap_sign_disagreement_pct" in columns:
                select_columns.append("overlap_sign_disagreement_pct")
            if "mean_abs_main_net_diff" in columns:
                select_columns.append("mean_abs_main_net_diff")
            recon_row = con.execute(
                f"SELECT {', '.join(select_columns)} FROM intraday_stock_flow_reconciliation "
                "WHERE trade_date=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
            if recon_row:
                recon_status = str(recon_row[0] or "not_run")
                recon_reference_rows = int(recon_row[1] or 0)
                if len(recon_row) > 2:
                    recon_value_status = str(recon_row[2] or "not_observed")
                if len(recon_row) > 3:
                    recon_sign_disagreement_pct = float(recon_row[3]) if recon_row[3] is not None else None
                if len(recon_row) > 4:
                    recon_mean_abs_main_net_diff = float(recon_row[4]) if recon_row[4] is not None else None
        if table_exists(con, "intraday_stock_flow_independent_reconciliation"):
            independent_rows = con.execute(
                "SELECT status, overlap_reference_pct, correlation_main_net, sign_agreement_pct, primary_provider, reference_provider "
                "FROM intraday_stock_flow_independent_reconciliation "
                "WHERE trade_date=CAST(? AS DATE)",
                [trade_date],
            ).fetchall()
            independent_row = independent_rows[0] if len(independent_rows) == 1 else None
            if independent_row:
                # Stay blocked even if reading or validating the evidence raises.
                stored_status = str(independent_row[0] or "not_run")
                independent_status = 'unverified_or_changed_amount_evidence'
                independent_overlap_pct = float(independent_row[1]) if independent_row[1] is not None else None
                independent_correlation = float(independent_row[2]) if independent_row[2] is not None else None
                independent_sign_agreement_pct = float(independent_row[3]) if independent_row[3] is not None else None
                from trade_system.flow_contract import independent_comparison_contract
                comparison_contract = independent_comparison_contract(con, trade_date, independent_row[4], independent_row[5])
                independent_reference_provider = independent_row[5]
                # A qualified new reference is consumed by its saved product,
                # never by a hardcoded vendor label or merely any dated row.
                from trade_system.readiness import _actual_flow_coverage
                independent_reference_qualification = _actual_flow_coverage(con, trade_date, "stock",
                    {"provider": independent_reference_provider, "expected_rows": expected_stock_codes},
                    now=now, collected_after=close_boundary)
                independent_source_present = bool(independent_reference_qualification["passed"])
                # Old correlation-only passes, and changed source rows, cannot
                # certify the new amount/scope rule through a stored green flag.
                import json
                from scripts.reconcile_independent_stock_flow import RULE_VERSION
                from trade_system.flow_contract import stock_flow_evidence_fingerprint
                columns = {r[1] for r in con.execute("PRAGMA table_info('intraday_stock_flow_independent_reconciliation')").fetchall()}
                qualified = False
                if {'evidence_json','rule_version'} <= columns:
                    encoded, version = con.execute('SELECT evidence_json,rule_version FROM '
                        'intraday_stock_flow_independent_reconciliation WHERE trade_date=?', [trade_date]).fetchone()
                    evidence = json.loads(encoded or '{}')
                    scope = qualified_stock_flow_scope(con, trade_date, now=now)
                    from trade_system.readiness import _finite_number
                    precision = comparison_contract.get('definition_evidence', {}).get('amount_precision', {})
                    quantums = [precision.get(k) for k in ('primary_quantum_yuan','reference_quantum_yuan')]
                    precision_valid = all(_finite_number(v) and v > 0 for v in quantums)
                    tolerance = sum(quantums)/2 if precision_valid else None
                    qualified = (version == RULE_VERSION and evidence.get('status') == 'pass'
                        and evidence.get('rule_version') == RULE_VERSION
                        and evidence.get('expected_rows',0) == expected_stock_codes
                        and scope['passed'] and evidence.get('expected_codes_sha256') == scope.get('codes_sha256')
                        and evidence.get('amount_match_pct',0) >= 99.5
                        and evidence.get('overlap_reference_pct',0) >= 99.5
                        and not evidence.get('unexpected_codes')
                        and precision_valid and evidence.get('absolute_tolerance_yuan') == tolerance
                        and evidence.get('comparison_contract', {}).get('definition_evidence') == comparison_contract.get('definition_evidence')
                        and independent_row[4] == primary_stock_provider
                        and all(evidence.get('source_fingerprints',{}).get(p) ==
                            stock_flow_evidence_fingerprint(con,trade_date,p) for p in independent_row[4:6]))
                if qualified and stored_status.lower() == 'pass':
                    independent_status = 'pass'
        con.close()
    except Exception:
        pass
    # A primary/reference comparison from the same Eastmoney family is useful
    # for transport consistency, but it is not independent accuracy evidence.
    # Keep it visible; only the saved, reviewed reference product can qualify.
    same_vendor_reconciliation_ready = (
        recon_status.lower() == "pass"
        and recon_value_status.lower() == "pass"
        and recon_reference_rows > 0
    )
    independent_reconciliation_ready = (
        independent_source_present
        and independent_status.lower() == "pass"
        and comparison_contract['eligible']
    )
    # Same-origin relay consistency and sector coverage are separate uses;
    # neither grants nor revokes a qualified independent stock comparison.
    stock_independent_certified_ready = stock_ready and independent_reconciliation_ready
    source_ready = stock_ready and sector_ready
    # Coverage is a source/pipeline property.  Independent reconciliation is a
    # certification property and must not be hidden behind the same ``ready``
    # label used by collectors.
    pipeline_ready = source_ready
    artifact_current = True
    flow_certified_ready = source_ready and independent_reconciliation_ready and not sector_taxonomy_stale
    operator_state = build_operator_state(
        source_ready=source_ready,
        pipeline_ready=pipeline_ready,
        artifact_current=artifact_current,
        data_certified_ready=source_ready and pipeline_ready and artifact_current,
        flow_certified_ready=flow_certified_ready,
        execution_ready=False,
        run_status="assessed",
        blockers=(
            (["stock_flow_not_ready"] if not stock_ready else [])
            + (["sector_flow_not_ready"] if not sector_ready else [])
        ),
        warnings=(
            (["sector_taxonomy_stale"] if sector_taxonomy_stale else [])
            + (["independent_flow_reconciliation_not_ready"]
               if not independent_reconciliation_ready else [])
        ),
    )
    operational_capabilities = build_operational_capabilities(
        trade_date=trade_date, as_of=now.isoformat(),
        stock_observation={"ready": stock_ready and stock_qualification.get("passed") is True,
            "qualification": stock_qualification,
            "blockers": [] if stock_ready else [stock_qualification.get("reason") or "stock_flow_not_ready"]},
        sector_observation={"ready": sector_ready and not sector_taxonomy_stale and sector_qualification.get("passed") is True,
            "qualification": sector_qualification,
            "blockers": [] if sector_ready and not sector_taxonomy_stale else
                [sector_qualification.get("reason") or "sector_flow_or_taxonomy_not_ready"]},
        flow_certified_ready=flow_certified_ready,
    )
    return {
        **operator_state,
        "operator_state": dict(operator_state),
        "operational_capabilities": operational_capabilities,
        "stock_independent_certified_ready": stock_independent_certified_ready,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "collection_started_at": collected_after.isoformat(timespec="seconds")
        if collected_after is not None
        else None,
        "min_coverage_pct": float(max(99.5, min_coverage_pct)),
        "max_age_seconds": int(max_age_seconds) if max_age_seconds is not None else None,
        "effective_max_age_seconds": (
            int(effective_max_age_seconds) if effective_max_age_seconds is not None else None
        ),
        "freshness_contract": "same_session_postclose_window" if session_close else "timestamp_ttl" if max_age_seconds is not None else "same_trade_date_no_ttl_certification",
        "evaluated_at": now.isoformat(timespec='seconds'),
        "stock_flow": {
            "ready": stock_ready,
            "observed_codes": stock_codes,
            "expected_codes": int(expected_stock_codes or 0),
            "coverage_pct": round(stock_coverage, 2) if stock_coverage is not None else None,
            "coverage_provider": primary_stock_provider,
            "relations": stock_relations,
            "qualification": stock_qualification,
        },
        "sector_flow": {
            "ready": sector_ready,
            "observed_codes": sector_codes,
            "expected_codes": int(expected_sector_codes or 0),
            "coverage_pct": round(sector_coverage, 2) if sector_coverage is not None else None,
            "relations": sector_relations,
            "qualification": sector_qualification,
            "taxonomy_stale": sector_taxonomy_stale,
            "taxonomy_stale_note": sector_taxonomy_note,
            "canonical_membership_snapshot": canonical_membership_snapshot,
            "derived_membership_snapshots": derived_membership_snapshots,
            "membership_batch_consistent": membership_batch_consistent,
        },
        "reconciliation": {
            "status": recon_status,
            "reference_rows": recon_reference_rows,
            "value_status": recon_value_status,
            "overlap_sign_disagreement_pct": recon_sign_disagreement_pct,
            "mean_abs_main_net_diff": recon_mean_abs_main_net_diff,
            "independent_source_present": independent_source_present,
            "independent_reference_provider": independent_reference_provider,
            "independent_reference_qualification": independent_reference_qualification,
            "same_vendor_reconciliation_ready": same_vendor_reconciliation_ready,
            "stock_independent_certified_ready": stock_independent_certified_ready,
            "independent_reconciliation_ready": independent_reconciliation_ready,
            "independent_status": independent_status,
            "comparison_contract": comparison_contract,
            "independent_overlap_pct": independent_overlap_pct,
            "independent_correlation_main_net": independent_correlation,
            "independent_sign_agreement_pct": independent_sign_agreement_pct,
        },
    }


def render_capital_flow_health_markdown(result: dict) -> str:
    lines = [
        "# Capital Flow Freshness",
        "",
        f"- Generated at: `{result['generated_at']}`",
        f"- Trade date: `{result['trade_date']}`",
        f"- Source ready: `{str(result.get('source_ready', result['ready'])).lower()}`",
        f"- Pipeline ready: `{str(result.get('pipeline_ready', False)).lower()}`",
        f"- Artifact current: `{str(result.get('artifact_current', False)).lower()}`",
        f"- Data certified ready: `{str(result.get('data_certified_ready', False)).lower()}`",
        f"- Flow certified ready: `{str(result.get('flow_certified_ready', False)).lower()}`",
        f"- Analysis ready: `{str(result.get('analysis_ready', result.get('certified_ready', False))).lower()}`",
        f"- Stock flow ready: `{str(result['stock_flow']['ready']).lower()}`",
        f"- Sector flow ready: `{str(result['sector_flow']['ready']).lower()}`",
        "",
        "| Scope | Relation | Rows | Codes | Gate rows | Gate codes | Source time | Latest fetch | Status |",
        "|---|---|---:|---:|---:|---:|---|---|---|",
    ]
    for scope in ("stock_flow", "sector_flow"):
        for item in result[scope]["relations"]:
            lines.append(
                f"| {scope} | {item['relation']} | {item['rows']} | {item['codes']} | "
                f"{item['recent_rows']} | {item['recent_codes']} | "
                f"{item['latest_source_time'] or ''} | {item['latest_timestamp'] or ''} | "
                f"{item['status']} |"
            )
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            f"- Minimum required: `{result['min_coverage_pct']}%` with dated scope and canonical product evidence",
            f"- Stock codes: `{result['stock_flow']['observed_codes']}` / "
            f"`{result['stock_flow']['expected_codes'] or 'not supplied'}` "
            f"(provider `{result['stock_flow'].get('coverage_provider') or 'best available'}`)",
            f"- Sector codes: `{result['sector_flow']['observed_codes']}` / "
            f"`{result['sector_flow']['expected_codes'] or 'not supplied'}`",
            "",
        ]
    )
    for name in ('stock_flow', 'sector_flow'):
        qualification = result[name].get('qualification', {})
        lines.append(f"- {name} shared qualification: `{str(qualification.get('passed', False)).lower()}`; "
                     f"reason `{qualification.get('reason') or 'qualified'}`")
    if result["sector_flow"].get("taxonomy_stale"):
        lines.append(
            f"- WARNING: THS concept membership stale -- "
            f"`{result['sector_flow'].get('taxonomy_stale_note', '')}` "
            "(concept taxonomy is not certified ready)"
        )
    lines.append(
        "- THS membership batch: canonical "
        f"`{result['sector_flow'].get('canonical_membership_snapshot') or 'none'}`, "
        f"derived `{result['sector_flow'].get('derived_membership_snapshots') or []}`, "
        "consistent "
        f"`{str(result['sector_flow'].get('membership_batch_consistent', True)).lower()}`"
    )
    recon = result.get("reconciliation", {})
    lines.extend([
        "",
        "## Independent reconciliation",
        "",
        f"- Same-vendor transport status: `{recon.get('status', 'not_run')}`",
        f"- Same-vendor value agreement: `{recon.get('value_status', 'not_observed')}` "
        f"(sign disagreement={recon.get('overlap_sign_disagreement_pct')}, "
        f"mean abs main-net diff={recon.get('mean_abs_main_net_diff')})",
        f"- Reference rows: `{recon.get('reference_rows', 0)}`",
        f"- Independent (TuShare) source present: "
        f"`{str(recon.get('independent_source_present', False)).lower()}`",
        f"- Cross-vendor persisted status: `{recon.get('independent_status', 'not_run')}`",
        f"- Independent overlap: `{recon.get('independent_overlap_pct')}`%",
        f"- Independent main-net correlation: `{recon.get('independent_correlation_main_net')}`",
        f"- Independent sign agreement: `{recon.get('independent_sign_agreement_pct')}`%",
        f"- Independent reconciliation ready: "
        f"`{str(recon.get('independent_reconciliation_ready', False)).lower()}`",
        f"- Same-vendor reconciliation ready: "
        f"`{str(recon.get('same_vendor_reconciliation_ready', False)).lower()}`",
    ])
    if not recon.get("independent_reconciliation_ready"):
        lines.append(
            "- WARNING: no passing independent reconciliation for this date "
            "(reference run absent/not_run or 0 reference rows); coverage passed on the "
            "primary source only -- accuracy is not independently verified."
        )
    if not recon.get("independent_source_present"):
        lines.append(
            "- WARNING: the saved reference product has no qualified same-date/full-scope rows; "
            "an alternative vendor label or relay does not certify independence."
        )
    return "\n".join(lines)
