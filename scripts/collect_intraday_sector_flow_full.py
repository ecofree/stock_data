"""Fetch and persist a full intraday sector-flow snapshot.

The fast KPL collector intentionally samples 20 sectors.  This job is the
separate all-sector tier: one paginated Eastmoney batch request per page,
source-aware persistence, a resumable-style batch checkpoint, and a strict
coverage report.  A failed/partial batch is visible to readiness and cannot be
silently replaced by the 20-sector compatibility rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
import math
import os
from pathlib import Path
import sys
import time
import uuid

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH, TODAY
from schema import init_schema
from trade_system.multi_source_store import MultiSourceStore
from trade_system.stock_data_sources import _from_em_sector_flow_page
from trade_system.capital_flow_health import assess_capital_flow_health, render_capital_flow_health_markdown
from trade_system.host_limiter import shared_host_limiter
from trade_system.concept_flow import _prepare_ths_aggregate as _prepare_ths_aggregate
from trade_system.concept_flow import _ths_membership_snapshot as _ths_membership_snapshot
from trade_system.concept_flow import THS_MEMBERSHIP_MAX_AGE_DAYS as THS_MEMBERSHIP_MAX_AGE_DAYS
from trade_system.units import _number, normalize_amount


SECTOR_COVERAGE_SUCCESS_PCT = 99.5

# THS concept membership is refreshed roughly weekly.  A membership snapshot older
# than this many days is too stale to certify the concept taxonomy as fully ready
# (A4): outdated membership is rejected before replacing the accepted aggregate.


def _compact(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _valid_rows(data, trade_date: str) -> list[dict]:
    if not isinstance(data, list):
        return []
    target = _compact(trade_date)
    flow_fields = ("main_net", "super_net", "large_net", "mid_net", "small_net")
    out = []
    for row in data:
        if not isinstance(row, dict) or not str(row.get("sector_code") or "").strip():
            continue
        row_dates = _compact(row.get("date") or row.get("trade_date"))
        if row_dates and row_dates != target:
            continue
        if not any(normalize_amount(row.get(field), row.get('amount_unit')) is not None for field in flow_fields):
            continue
        out.append(row)
    return list({str(row["sector_code"]): row for row in out}.values())


def _collect_sector_pages(store, trade_date, page_size, max_pages, pause_seconds,
                          expected_codes=None, catalogue_version=None):
    """Stage a bounded batch; never infer membership from matching row counts."""
    expected = set(expected_codes or [])
    if expected_codes is not None and (not expected or len(expected) != len(expected_codes)
            or not isinstance(catalogue_version, str) or not 1 <= len(catalogue_version) <= 256 or len(expected) > 10000
            or any(not isinstance(code, str) or not 1 <= len(code) <= 64 for code in expected)):
        raise ValueError('unique bounded expected codes and catalogue version required')
    staged, totals, errors, cursors = {}, set(), [], []
    invalid, duplicates, requests, primary_pages, reverse_pages = 0, 0, 0, 0, 0
    transport_size = 0
    for direction, attempts in [('0', 3), ('1', 2)]:
        total = max(totals, default=0)
        if direction == '1' and (not total or not staged or len(staged) >= total):
            break
        for page in range(1, max_pages + 1):
            raw, meta = None, {}
            for attempt in range(attempts):
                requests += 1
                try:
                    shared_host_limiter.acquire('eastmoney', max(float(pause_seconds), 1.0))
                    raw, meta = _from_em_sector_flow_page(page=page, page_size=page_size,
                        return_meta=True, sort_field='f12', sort_order=direction)
                    break
                except Exception as exc:
                    errors.append(str(exc)[:300])
                    if attempt + 1 < attempts:
                        time.sleep(min(5.0, 1.5*(attempt+1)))
            cursor = {'direction':direction, 'page':page}
            cursors.append(cursor)
            if direction == '0': primary_pages += 1
            else: reverse_pages += 1
            if not isinstance(raw, list) or len(raw) > page_size:
                errors.append('missing_or_oversized_page')
                store.store('sector_flow_page', None, {'cursor':cursor,'error':errors[-1]},
                    {'source':'eastmoney_sector_full','status':'failed'}, trade_date=trade_date)
                break
            if not isinstance(meta, dict): meta = {}
            store.store('sector_flow_page', None, {'cursor':cursor,'rows':raw,'meta':meta},
                {'source':'eastmoney_sector_full','status':'observed'}, trade_date=trade_date)
            raw_total = _number(meta.get('total'))
            if raw_total is not None and 0 < raw_total <= 10000 and raw_total.is_integer():
                totals.add(int(raw_total))
            else:
                errors.append('invalid_or_missing_total')
            if len(totals) > 1:
                errors.append('catalogue_total_changed_during_capture')
            if not raw: break
            transport_size = transport_size or len(raw)
            valid = _valid_rows(raw, trade_date)
            # Duplicated codes are not missing data, but conflicting taxonomies
            # or malformed values are. Raw page evidence above is retained.
            unique_raw = {str(r.get('sector_code')) for r in raw if isinstance(r, dict) and r.get('sector_code')}
            invalid += sum(not isinstance(r, dict) or not r.get('sector_code') for r in raw)
            invalid += len(unique_raw - {str(r['sector_code']) for r in valid})
            for row in valid:
                if row.get('sector_type') != 'em_industry':
                    invalid += 1
                    continue
                code = str(row['sector_code'])
                duplicates += int(code in staged)
                staged.setdefault(code, row)
            total = max(totals, default=0)
            if total and len(staged) >= total: break
            if total and page >= math.ceil(total / transport_size): break
            if not total and (len(raw) < page_size or page > 1): break
            if pause_seconds > 0: time.sleep(float(pause_seconds))
    codes = set(staged)
    total = max(totals, default=0)
    missing = sorted(expected - codes) if expected_codes is not None else []
    unexpected = sorted(codes - expected) if expected_codes is not None else []
    count_complete = bool(total) and len(codes) == total
    if len(totals) > 1 or invalid or missing or unexpected:
        status = 'partial'
    elif not count_complete:
        status = 'budget_exhausted' if primary_pages >= max_pages else 'partial'
    elif any(e in ('invalid_or_missing_total','missing_or_oversized_page') for e in errors):
        status = 'unverified'
    elif expected_codes is None:
        status = 'unverified_catalogue'
    else:
        status = 'complete'
    membership = {'taxonomy':'em_industry','version':catalogue_version,'codes':sorted(expected)}
    return list(staged.values()), {
        'status':status,'promoted':False,'requests':requests,'max_requests':max_pages*5,
        'primary_pages':primary_pages,'reconciliation_pages':reverse_pages,
        'expected_pages':math.ceil(total/transport_size) if total and transport_size else 0,
        'expected_total':total,'observed_codes':len(codes),'invalid_rows':invalid,
        'duplicate_rows':duplicates,'total_values':sorted(totals),'errors':errors,
        'missing_codes':missing,'unexpected_codes':unexpected,'catalogue_version':catalogue_version,
        'catalogue_sha256':hashlib.sha256(json.dumps(membership,sort_keys=True).encode()).hexdigest() if expected_codes is not None else None,
        'resume_cursor':None if status=='complete' else (cursors[-1] if cursors else None),
        'cursor_scope':'diagnostic_restart_whole_capture_no_cross_snapshot_page_stitching',
    }


def _replace_sector_snapshot(store, trade_date, rows, pagination):
    """Replace one provider/date atomically after membership qualification."""
    if pagination['status'] != 'complete': return False
    con = store.con
    con.execute('BEGIN TRANSACTION')
    try:
        con.execute("DELETE FROM multi_source_sector_flow WHERE source_date=CAST(? AS DATE) AND provider='eastmoney_sector_full'", [trade_date])
        qualified = [dict(row, catalogue_version=pagination['catalogue_version']) for row in rows]
        result = store.store('sector_flow', None, qualified,
            {'source':'eastmoney_sector_full','status':'live'}, trade_date=trade_date, commit=False)
        if result['rows_written'] != pagination['expected_total']:
            raise ValueError('qualified page rows changed before publication')
        con.commit()
    except Exception:
        con.rollback()
        raise
    pagination['promoted'] = True
    return True


def _taxonomy_coverage_status(
    expected: int,
    observed: int,
    *,
    unverified: bool = False,
) -> tuple[float, str]:
    coverage = round(observed * 100.0 / expected, 2) if expected else 0.0
    if unverified and observed:
        return coverage, "unverified"
    if expected and coverage >= SECTOR_COVERAGE_SUCCESS_PCT:
        return coverage, "success"
    return coverage, "partial" if observed else "missing"


def _expected_sector_taxonomies(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, int]:
    """Return independent denominators; never combine sector code spaces."""
    out = {"em_industry": 0, "tushare_dc_sector": 0, "ths_concept": 0}
    try:
        out["ths_concept"] = int(con.execute(
            "SELECT count(DISTINCT concept_code) FROM v_default_concept_daily "
            "WHERE trade_date=(SELECT max(trade_date) FROM v_default_concept_daily WHERE trade_date<=CAST(? AS DATE))",
            [trade_date],
        ).fetchone()[0] or 0)
    except Exception:
        pass
    try:
        out["tushare_dc_sector"] = int(con.execute(
            "SELECT count(DISTINCT ts_code) FROM tushare_moneyflow_industry WHERE trade_date=CAST(? AS DATE)",
            [trade_date],
        ).fetchone()[0] or 0)
    except Exception:
        pass
    return out




def _aggregate_ths_stock_flow(con, trade_date):
    """Compatibility read helper: partial inputs no longer yield a total."""
    return _prepare_ths_aggregate(con, trade_date)[0]


def _publish_ths_aggregate(store, trade_date, *, now=None, max_age_seconds=10800):
    """Both collector and recovery use one consistent read/write transaction."""
    con = store.con
    con.execute("BEGIN TRANSACTION")
    try:
        rows, report = _prepare_ths_aggregate(
            con, trade_date, now=now, max_age_seconds=max_age_seconds)
        if report["status"] == "complete":
            con.execute("DELETE FROM multi_source_sector_flow WHERE source_date=CAST(? AS DATE) "
                        "AND provider='derived_ths_stock_aggregate'", [trade_date])
            stored = store.store("sector_flow", None, rows,
                                 {"source":"derived_ths_stock_aggregate", "status":"live"},
                                 trade_date=trade_date, commit=False)
            if stored["rows_written"] != report["expected_rows"]:
                raise ValueError("THS aggregate write count mismatch")
            report.update(status="success", promoted=True, rows_written=stored["rows_written"])
        con.commit()
    except Exception as exc:
        con.rollback()
        report = dict(status="error", promoted=False, expected_rows=0, fetched_rows=0,
                      rows_written=0, coverage_pct=0.0, error=str(exc)[:500],
                      ths_membership_snapshot=None, ths_membership_age_days=None)
    # Failed attempts are observable without replacing a prior successful slice.
    store.store("ths_aggregate_batch", None, report,
                {"source":"derived_ths_stock_aggregate", "status":report["status"]},
                trade_date=trade_date)
    return report


def rebuild_ths_derived_flow(db_path: str | Path, trade_date: str, *, now=None,
                             max_age_seconds=10800) -> dict:
    """Offline recovery; failure retains the last good slice. No remote calls."""
    with MultiSourceStore(db_path) as store:
        init_schema(store.con)
        _ensure_batch_table(store.con)
        result = _publish_ths_aggregate(store, trade_date, now=now,
                                        max_age_seconds=max_age_seconds)
        store.con.execute("""
            INSERT INTO intraday_sector_flow_taxonomy(
                trade_date,taxonomy,provider,expected_rows,fetched_rows,
                coverage_pct,status,last_error,updated_at)
            VALUES (CAST(? AS DATE),'ths_concept','derived_ths_stock_aggregate',?,?,?,?,?,current_timestamp)
            ON CONFLICT(trade_date,taxonomy) DO UPDATE SET
                expected_rows=excluded.expected_rows, fetched_rows=excluded.fetched_rows,
                coverage_pct=excluded.coverage_pct, status=excluded.status,
                last_error=excluded.last_error, updated_at=now()
        """, [trade_date, result["expected_rows"], result["fetched_rows"],
              result["coverage_pct"], result["status"], result["error"]])
        store.con.commit()
    age = result["ths_membership_age_days"]
    return dict(result, trade_date=trade_date, provider="derived_ths_stock_aggregate",
                ths_members_stale=age is None or age > THS_MEMBERSHIP_MAX_AGE_DAYS,
                taxonomy_status={"ths_concept":{key:result[key] for key in
                    ("expected_rows","fetched_rows","coverage_pct","status")}},
                fetched_pages=0, expected_pages=0, reconciliation_pages=0)


def _ensure_batch_table(con: duckdb.DuckDBPyConnection) -> None:
    if os.environ.get("KPL_RUNTIME_SCHEMA_READY", "").strip() == "1":
        return
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_sector_flow_batch (
            trade_date DATE PRIMARY KEY,
            run_id VARCHAR,
            provider VARCHAR,
            expected_rows INTEGER,
            fetched_rows INTEGER,
            expected_pages INTEGER,
            fetched_pages INTEGER,
            coverage_pct DOUBLE,
            status VARCHAR,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_sector_flow_taxonomy (
            trade_date DATE,
            taxonomy VARCHAR,
            provider VARCHAR,
            expected_rows INTEGER,
            fetched_rows INTEGER,
            coverage_pct DOUBLE,
            status VARCHAR,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(trade_date, taxonomy)
        )
        """
    )


def collect_full_sector_flow(
    db_path: str | Path,
    trade_date: str,
    *,
    page_size: int = 500,
    max_pages: int = 20,
    pause_seconds: float = 0.35,
    expected_codes: list[str] | None = None,
    catalogue_version: str | None = None,
) -> dict:
    run_id = f"em_sector_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    page_size = max(50, min(int(page_size), 5000))
    max_pages = int(max_pages)
    if not 1 <= max_pages <= 32:
        raise ValueError('sector capture requires one to 32 pages per sweep')
    # Refresh only the taxonomies produced by this collector.  Same-day
    # TuShare DC industry rows are an independent normalized source and must
    # survive an Eastmoney refresh.
    fetched_pages = 0
    expected_pages = 0
    error = ""
    provider_used = ""
    taxonomy_status = {}
    em_total = 0
    pagination = {'status':'not_started', 'promoted':False}
    reconciliation_pages = 0
    ths_snap = None
    ths_age = None
    ths_members_stale = False
    ths_result = {}

    with MultiSourceStore(db_path) as store:
        con = store.con
        init_schema(con)
        _ensure_batch_table(con)
        expected_taxonomies = _expected_sector_taxonomies(con, trade_date)
        expected_rows = sum(expected_taxonomies.values())
        con.execute("DELETE FROM intraday_sector_flow_taxonomy WHERE trade_date=?", [trade_date])
        for taxonomy, expected in expected_taxonomies.items():
            con.execute(
                "INSERT INTO intraday_sector_flow_taxonomy(trade_date,taxonomy,provider,expected_rows,fetched_rows,coverage_pct,status,last_error) VALUES (?,?,?,?,?,?,?,?)",
                [trade_date, taxonomy, taxonomy, expected, 0, 0.0, "running", ""],
            )
        con.execute("DELETE FROM intraday_sector_flow_batch WHERE trade_date=?", [trade_date])
        # Preserve the prior snapshot until the complete staged membership
        # has passed its own contract; old rows never count toward this batch.
        con.execute(
            "INSERT INTO intraday_sector_flow_batch(trade_date,run_id,provider,status,updated_at) VALUES (?,?,?,?,current_timestamp)",
            [trade_date, run_id, "eastmoney_sector_full", "running"],
        )
        con.commit()

        try:
            staged_rows, pagination = _collect_sector_pages(
                store, trade_date, page_size, max_pages, pause_seconds,
                expected_codes, catalogue_version)
            em_total = pagination['expected_total']
            expected_taxonomies['em_industry'] = em_total
            expected_pages = pagination['expected_pages']
            fetched_pages = pagination['primary_pages']
            reconciliation_pages = pagination['reconciliation_pages']
            error = '; '.join(pagination['errors'])[:500]
            if _replace_sector_snapshot(store, trade_date, staged_rows, pagination):
                provider_used = 'eastmoney_sector_full'
            # Failed/count-only captures remain observations. Never rename an
            # arbitrary resilient fallback as a different sector taxonomy.
            store.store('sector_flow_batch', None, pagination,
                {'source':'eastmoney_sector_full','status':pagination['status']},
                trade_date=trade_date)

            ths_result = _publish_ths_aggregate(store, trade_date)
            ths_snap = ths_result["ths_membership_snapshot"]
            ths_age = ths_result["ths_membership_age_days"]
            ths_members_stale = ths_age is None or ths_age > THS_MEMBERSHIP_MAX_AGE_DAYS
            retained_ths = con.execute("SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                "WHERE source_date=CAST(? AS DATE) AND provider='derived_ths_stock_aggregate'", [trade_date]).fetchone()[0]
            expected_taxonomies["ths_concept"] = ths_result["expected_rows"] or max(
                expected_taxonomies["ths_concept"], retained_ths)
            if ths_result["promoted"]:
                provider_used = "+".join(filter(None, (provider_used, "derived_ths_stock_aggregate")))

            taxonomy_providers = {
                "em_industry": "eastmoney_sector_full",
                "tushare_dc_sector": "tushare_sector_full",
                "ths_concept": "derived_ths_stock_aggregate",
            }
            taxonomy_rows = {}
            for taxonomy, provider in taxonomy_providers.items():
                observed = int(con.execute(
                    "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                    "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE AND provider=?",
                    [trade_date, provider],
                ).fetchone()[0])
                expected = int(expected_taxonomies.get(taxonomy) or 0)
                unverified = taxonomy == "em_industry" and not em_total
                if taxonomy == "em_industry" and expected == 0 and observed:
                    # Rows without an API total are observable, but not proof
                    # of complete coverage.  Keep the state unverified.
                    expected = observed
                if taxonomy == "em_industry" and em_total:
                    expected = em_total
                tax_coverage, tax_status = _taxonomy_coverage_status(
                    expected,
                    observed,
                    unverified=unverified,
                )
                if taxonomy == 'em_industry':
                    observed = pagination.get('observed_codes', 0)
                    tax_coverage = round(observed * 100 / expected, 2) if expected else 0.0
                    tax_status = 'success' if pagination.get('promoted') else pagination['status']
                tax_error = "unverified denominator" if unverified else ""
                if taxonomy == "ths_concept":
                    observed = ths_result["fetched_rows"]
                    tax_coverage = ths_result["coverage_pct"]
                    tax_status = ths_result["status"]
                    if tax_status == "missing_inputs" and expected == 0:
                        tax_status = "missing"
                    tax_error = ths_result["error"]
                taxonomy_rows[taxonomy] = (expected, observed, tax_coverage, tax_status)
                con.execute(
                    "UPDATE intraday_sector_flow_taxonomy SET provider=?,expected_rows=?,fetched_rows=?,coverage_pct=?,status=?,last_error=?,updated_at=current_timestamp WHERE trade_date=? AND taxonomy=?",
                    [provider, expected, observed, tax_coverage,
                     tax_status, tax_error, trade_date, taxonomy],
                )
            expected_rows = sum(item[0] for item in taxonomy_rows.values())
            fetched_rows = sum(item[1] for item in taxonomy_rows.values())
            known_coverages = [item[2] for item in taxonomy_rows.values() if item[0]]
            coverage = min(known_coverages) if known_coverages else 0.0
            taxonomy_status = {
                taxonomy: {"expected_rows": item[0], "fetched_rows": item[1],
                           "coverage_pct": item[2], "status": item[3]}
                for taxonomy, item in taxonomy_rows.items()
            }
            required_taxonomies = [item for item in taxonomy_rows.values() if item[0] > 0]
            optional_gap = any(item[0] == 0 and item[3] == "missing" for item in taxonomy_rows.values())
            required_complete = bool(required_taxonomies) and all(
                item[3] == "success"
                and item[2] >= SECTOR_COVERAGE_SUCCESS_PCT
                for item in required_taxonomies
            )
            status = (
                "success_with_optional_gap" if required_complete and optional_gap
                else "success" if required_complete
                else "partial" if fetched_rows
                else "error"
            )
            if not pagination.get('promoted'):
                status = 'partial' if fetched_rows else 'error'
            if ths_result['status'] not in {'success', 'missing_inputs'}:
                status = 'partial' if fetched_rows else 'error'
        except Exception as exc:
            error = str(exc)[:500]
            fetched_rows = int(con.execute(
                "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE AND provider IN ('eastmoney_sector_full','tushare_sector_full','derived_ths_stock_aggregate')",
                [trade_date],
            ).fetchone()[0])
            coverage = min(100.0, round(fetched_rows * 100.0 / expected_rows, 2)) if expected_rows else 0.0
            status = "partial" if fetched_rows else "error"

        if status in {"success", "success_with_optional_gap"}:
            try:
                store.sync_sector_capital(trade_date)
            except ValueError as exc:
                status = 'partial'
                error = str(exc)[:500]
        con.execute(
            "UPDATE intraday_sector_flow_batch SET expected_rows=?,fetched_rows=?,expected_pages=?,fetched_pages=?,coverage_pct=?,status=?,last_error=?,provider=?,updated_at=current_timestamp WHERE trade_date=?",
            [expected_rows, fetched_rows, expected_pages, fetched_pages, coverage, status, error, provider_used or "eastmoney_sector_full", trade_date],
        )
        con.commit()

    return {
        "trade_date": trade_date,
        "run_id": run_id,
        "provider": provider_used or "eastmoney_sector_full",
        "status": status,
        "expected_rows": expected_rows,
        "fetched_rows": fetched_rows,
        "expected_pages": expected_pages,
        "fetched_pages": fetched_pages,
        "coverage_pct": coverage,
        "error": error,
        "reconciliation_pages": reconciliation_pages,
        "taxonomy_status": taxonomy_status,
        "ths_membership_snapshot": str(ths_snap) if ths_snap else None,
        "ths_membership_age_days": ths_age,
        "ths_members_stale": ths_members_stale,
        "pagination": pagination,
        "ths_aggregation": ths_result,
    }


def render_report(result: dict) -> str:
    return "\n".join([
        "# Intraday Full-Market Sector Capital Flow",
        "",
        f"- trade_date: `{result['trade_date']}`",
        f"- provider: `{result['provider']}`",
        f"- status: `{result['status']}`",
        f"- pages: `{result['fetched_pages']}/{result['expected_pages']}`",
        f"- sector coverage: `{result['fetched_rows']}/{result['expected_rows']}` ({result['coverage_pct']}%)",
        f"- reverse-code reconciliation pages: `{result.get('reconciliation_pages', 0)}`",
        f"- error: `{result.get('error') or ''}`",
        f"- THS membership snapshot: `{result.get('ths_membership_snapshot') or 'none'}` "
        f"(age `{result.get('ths_membership_age_days')}`d, stale=`{result.get('ths_members_stale')}`)",
        "",
        "## Taxonomy coverage",
        "",
        "| taxonomy | fetched / expected | coverage | status |",
        "|---|---:|---:|---|",
        *[
            f"| {name} | {item.get('fetched_rows', 0)}/{item.get('expected_rows', 0)} | {item.get('coverage_pct', 0)}% | {item.get('status', '')} |"
            for name, item in sorted((result.get('taxonomy_status') or {}).items())
        ],
        "",
        "The 20-sector KPL fast tier remains separate. Taxonomy rows are not combined for readiness.",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and persist full-market intraday sector capital flow.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--pause-seconds", type=float, default=0.35)
    parser.add_argument('--catalogue', type=Path, help='Frozen JSON: trade_date, taxonomy=em_industry, version, codes; absent means observations only')
    parser.add_argument("--out", default="reports/intraday_sector_flow_latest.md")
    parser.add_argument(
        "--ths-derived-only",
        action="store_true",
        help="Rebuild THS concept aggregates from persisted same-date stock flow without remote collection.",
    )
    args = parser.parse_args()
    if args.date != date.today().isoformat():
        print(f"date={args.date} status=historical_collection_blocked")
        return 2
    catalogue = json.loads(args.catalogue.read_text(encoding='utf-8')) if args.catalogue else {}
    if catalogue and (catalogue.get('trade_date') != args.date or catalogue.get('taxonomy') != 'em_industry'):
        parser.error('catalogue must bind the requested date and em_industry namespace')
    result = (
        rebuild_ths_derived_flow(args.db, args.date)
        if args.ths_derived_only
        else collect_full_sector_flow(
            args.db, args.date, page_size=args.page_size,
            max_pages=args.max_pages, pause_seconds=args.pause_seconds,
            expected_codes=catalogue.get('codes'), catalogue_version=catalogue.get('version'),
        )
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    health = assess_capital_flow_health(
        args.db, args.date, expected_sector_codes=result["expected_rows"],
        min_coverage_pct=80.0,
    )
    health_path = out.parent / "capital_flow_freshness_latest.md"
    health_path.write_text(render_capital_flow_health_markdown(health), encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()), f"report={out}")
    return 0 if result["status"] in {"success", "success_with_optional_gap"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
