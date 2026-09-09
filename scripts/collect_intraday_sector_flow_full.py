"""Fetch and persist a full intraday sector-flow snapshot.

The fast KPL collector intentionally samples 20 sectors.  This job is the
separate all-sector tier: one paginated Eastmoney batch request per page,
source-aware persistence, a resumable-style batch checkpoint, and a strict
coverage report.  A failed/partial batch is visible to readiness and cannot be
silently replaced by the 20-sector compatibility rows.
"""

from __future__ import annotations

import argparse
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
from trade_system import resilient_sources
from trade_system.capital_flow_health import assess_capital_flow_health, render_capital_flow_health_markdown
from trade_system.host_limiter import shared_host_limiter
from trade_system.quality import table_exists
from trade_system.source_authority import provider_rank_sql


SECTOR_COVERAGE_SUCCESS_PCT = 99.5

# THS concept membership is refreshed roughly weekly.  A membership snapshot older
# than this many days is too stale to certify the concept taxonomy as fully ready
# (A4): the derived concept flows are still persisted, but the membership behind
# them is out of date and must be surfaced to the readiness gate/report rather than
# being silently judged 100% complete.
THS_MEMBERSHIP_MAX_AGE_DAYS = 10


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
        if not any(row.get(field) not in (None, "", "-") for field in flow_fields):
            continue
        out.append(row)
    return list({str(row["sector_code"]): row for row in out}.values())


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


def _ths_membership_snapshot(con: duckdb.DuckDBPyConnection, trade_date: str):
    """Return (snapshot_date, age_days) for the THS concept membership in effect on
    trade_date, or (None, None) when no membership snapshot exists at or before it."""
    try:
        # Prefer the canonical quality-gated view.  Small legacy/test databases
        # may predate that view, in which case the raw table is the only
        # available relation; production schema initialization always creates
        # the view, so stale/partial rows cannot silently re-enter there.
        relation = (
            "v_default_concept_stock_history"
            if table_exists(con, "v_default_concept_stock_history")
            else "ths_concept_stock_history"
        )
        row = con.execute(
            f"SELECT max(trade_date) FROM {relation} "
            "WHERE trade_date<=CAST(? AS DATE)",
            [trade_date],
        ).fetchone()
        snap = row[0] if row else None
        if snap is None:
            return None, None
        snap_d = snap if isinstance(snap, date) else date.fromisoformat(str(snap)[:10])
        trade_d = date.fromisoformat(str(trade_date)[:10])
        return snap_d, (trade_d - snap_d).days
    except Exception:
        return None, None


def _aggregate_ths_stock_flow(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict]:
    """Derive concept flow from the already complete same-day stock flow.

    This is a transparent fallback, not a fabricated quote: each concept row
    is a sum of member-stock directional buckets and carries its member count
    in ``raw`` for auditability.
    """
    try:
        provider_order = provider_rank_sql("stock_flow", "provider")
        rows = con.execute(
            f"""
            WITH ranked_stock_flow AS (
                SELECT *, row_number() OVER (
                    PARTITION BY stock_code
                    ORDER BY {provider_order} ASC,
                        fetched_at DESC
                ) AS provider_rank
                FROM multi_source_stock_flow
                WHERE source_date=CAST(? AS DATE)
                  AND provider IN ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay')
                  AND is_stale=FALSE
            ), concept_members AS (
                SELECT DISTINCT concept_code, concept_name, trade_date AS snapshot_date,
                       regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code
                FROM v_default_concept_stock_history
                WHERE trade_date=(
                    SELECT max(trade_date) FROM v_default_concept_stock_history
                    WHERE trade_date<=CAST(? AS DATE)
                )
            )
            SELECT c.concept_code, c.concept_name,
                   sum(s.main_net) AS main_net,
                   sum(s.super_net) AS super_net,
                   sum(s.large_net) AS large_net,
                   sum(s.mid_net) AS mid_net,
                   sum(s.small_net) AS small_net,
                   avg(s.change_pct) AS change_pct,
                   count(DISTINCT s.stock_code) AS stock_count,
                   max(c.snapshot_date) AS membership_snapshot_date
            FROM concept_members c
            JOIN ranked_stock_flow s
              ON CAST(s.stock_code AS VARCHAR) = c.stock_code
             AND s.provider_rank=1
            
            GROUP BY c.concept_code, c.concept_name
            HAVING count(DISTINCT s.stock_code) > 0
            """,
            [trade_date, trade_date],
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "sector_code": str(row[0]),
            "sector_name": row[1] or "",
            "main_net": row[2],
            "super_net": row[3],
            "large_net": row[4],
            "mid_net": row[5],
            "small_net": row[6],
            "change_pct": row[7],
            "sector_type": "ths_concept_derived",
            "amount_unit": "yuan",
            "raw": {"derived_from": "multi_source_stock_flow", "member_stock_count": int(row[8] or 0),
                    "membership_snapshot_date": str(row[9])},
        }
        for row in rows
    ]


def rebuild_ths_derived_flow(db_path: str | Path, trade_date: str) -> dict:
    """Rebuild only the deterministic THS aggregate from persisted stock flow.

    This is the recovery path after a report/control-plane failure.  It does
    not call a remote provider and it replaces the complete provider/date
    slice so concepts removed from the new membership snapshot cannot linger.
    """
    with MultiSourceStore(db_path) as store:
        con = store.con
        init_schema(con)
        _ensure_batch_table(con)
        snapshot, age = _ths_membership_snapshot(con, trade_date)
        rows = _aggregate_ths_stock_flow(con, trade_date)
        if not snapshot or not rows:
            return {
                "trade_date": trade_date,
                "provider": "derived_ths_stock_aggregate",
                "status": "missing_inputs",
                "fetched_rows": 0,
                "expected_rows": 0,
                "coverage_pct": 0.0,
                "ths_membership_snapshot": str(snapshot) if snapshot else None,
                "ths_membership_age_days": age,
                "error": "complete THS membership or same-date stock flow unavailable",
                "taxonomy_status": {},
                "fetched_pages": 0,
                "expected_pages": 0,
                "reconciliation_pages": 0,
                "ths_members_stale": True,
            }
        expected = int(con.execute(
            "SELECT count(DISTINCT concept_code) FROM v_default_concept_stock_history "
            "WHERE trade_date=CAST(? AS DATE)",
            [snapshot],
        ).fetchone()[0] or 0)
        con.execute(
            "DELETE FROM multi_source_sector_flow "
            "WHERE source_date=CAST(? AS DATE) AND provider='derived_ths_stock_aggregate'",
            [trade_date],
        )
        stored = store.store(
            "sector_flow", None, rows,
            {"source": "derived_ths_stock_aggregate", "status": "live", "trade_date": trade_date},
            asset_type="sector", trade_date=trade_date,
        )
        observed = int(con.execute(
            "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
            "WHERE source_date=CAST(? AS DATE) "
            "AND provider='derived_ths_stock_aggregate' AND coalesce(is_stale,FALSE)=FALSE",
            [trade_date],
        ).fetchone()[0] or 0)
        coverage, taxonomy_state = _taxonomy_coverage_status(expected, observed)
        con.execute(
            """
            INSERT INTO intraday_sector_flow_taxonomy(
                trade_date,taxonomy,provider,expected_rows,fetched_rows,
                coverage_pct,status,last_error,updated_at
            ) VALUES (CAST(? AS DATE),'ths_concept','derived_ths_stock_aggregate',?,?,?,?,?,current_timestamp)
            ON CONFLICT(trade_date,taxonomy) DO UPDATE SET
                provider=excluded.provider, expected_rows=excluded.expected_rows,
                fetched_rows=excluded.fetched_rows, coverage_pct=excluded.coverage_pct,
                status=excluded.status, last_error=excluded.last_error,
                updated_at=now()
            """,
            [trade_date, expected, observed, coverage, taxonomy_state, ""],
        )
        con.commit()
        return {
            "trade_date": trade_date,
            "provider": "derived_ths_stock_aggregate",
            "status": taxonomy_state,
            "fetched_rows": observed,
            "expected_rows": expected,
            "coverage_pct": coverage,
            "rows_written": int(stored.get("rows_written") or 0),
            "ths_membership_snapshot": str(snapshot),
            "ths_membership_age_days": age,
            "error": "",
            "taxonomy_status": {
                "ths_concept": {
                    "expected_rows": expected,
                    "fetched_rows": observed,
                    "coverage_pct": coverage,
                    "status": taxonomy_state,
                }
            },
            "fetched_pages": 1,
            "expected_pages": 1,
            "reconciliation_pages": 0,
            "ths_members_stale": bool(age is None or age > THS_MEMBERSHIP_MAX_AGE_DAYS),
        }


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
) -> dict:
    run_id = f"em_sector_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    page_size = max(50, min(int(page_size), 5000))
    max_pages = max(1, int(max_pages))
    # Refresh only the taxonomies produced by this collector.  Same-day
    # TuShare DC industry rows are an independent normalized source and must
    # survive an Eastmoney refresh.
    providers = {"eastmoney_sector_full", "derived_ths_stock_aggregate"}
    fetched_pages = 0
    expected_pages = 0
    error = ""
    provider_used = ""
    taxonomy_status = {}
    em_total = 0
    transport_page_size = 0
    reconciliation_pages = 0
    ths_snap = None
    ths_age = None
    ths_members_stale = False

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
        # Keep the last valid full-tier snapshot until the first usable page
        # of this refresh arrives.  A remote reset must not erase a prior
        # intraday sector snapshot.
        refresh_cleared = False
        con.execute(
            "INSERT INTO intraday_sector_flow_batch(trade_date,run_id,provider,status,updated_at) VALUES (?,?,?,?,current_timestamp)",
            [trade_date, run_id, "eastmoney_sector_full", "running"],
        )
        con.commit()

        try:
            for page in range(1, max_pages + 1):
                before_rows = int(con.execute(
                    "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                    "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE "
                    "AND provider='eastmoney_sector_full'",
                    [trade_date],
                ).fetchone()[0])
                raw = None
                page_meta = {}
                page_error = ""
                for attempt in range(3):
                    try:
                        shared_host_limiter.acquire("eastmoney", max(float(pause_seconds), 1.0))
                        raw, page_meta = _from_em_sector_flow_page(
                            page=page, page_size=page_size, return_meta=True,
                            sort_field="f12", sort_order="0",
                        )
                        if page_meta.get("total"):
                            em_total = max(em_total, int(page_meta["total"]))
                            expected_taxonomies["em_industry"] = em_total
                        if raw:
                            break
                    except Exception as exc:
                        page_error = str(exc)[:500]
                        time.sleep(min(8.0, 1.5 * (attempt + 1)))
                if raw is None:
                    error = page_error or "empty sector-flow page"
                    break
                if not raw:
                    break
                if not transport_page_size:
                    transport_page_size = len(raw)
                if em_total and transport_page_size:
                    expected_pages = max(
                        expected_pages,
                        int(math.ceil(em_total / transport_page_size)),
                    )
                else:
                    expected_pages = max(expected_pages, page)
                rows = _valid_rows(raw, trade_date)
                if rows:
                    if not refresh_cleared:
                        for provider in providers:
                            con.execute(
                                "DELETE FROM multi_source_sector_flow WHERE source_date=CAST(? AS DATE) AND provider=?",
                                [trade_date, provider],
                            )
                        con.commit()
                        refresh_cleared = True
                    stored = store.store(
                        "sector_flow",
                        None,
                        rows,
                        {"source": "eastmoney_sector_full", "status": "live", "trade_date": trade_date},
                        asset_type="sector",
                        trade_date=trade_date,
                    )
                    if stored.get("rows_written"):
                        provider_used = "eastmoney_sector_full"
                fetched_pages = page
                after_rows = int(con.execute(
                    "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                    "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE "
                    "AND provider='eastmoney_sector_full'",
                    [trade_date],
                ).fetchone()[0])
                # Eastmoney currently caps a page at 100 even when pz=500.
                # Do not mistake that transport cap for the end of the
                # universe; stop only on an empty page, the known page count,
                # or a proven expected-universe match.  A duplicate-only page
                # is not a safe stop when the upstream total is known.
                if not raw:
                    break
                if expected_taxonomies.get("em_industry") and after_rows >= expected_taxonomies["em_industry"]:
                    break
                if expected_pages and page >= expected_pages:
                    break
                if page > 1 and after_rows <= before_rows and not em_total:
                    break
                if pause_seconds > 0:
                    time.sleep(float(pause_seconds))

            fetched_rows = int(con.execute(
                "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE AND provider IN ('eastmoney_sector_full','tushare_sector_full','derived_ths_stock_aggregate')",
                [trade_date],
            ).fetchone()[0])

            # If Eastmoney is blocked, use the existing resilient graph once
            # as a bounded fallback.  It still carries the same strict flow
            # validation and is marked partial when it cannot cover the known
            # universe.
            em_rows_now = int(con.execute(
                "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE AND provider='eastmoney_sector_full'",
                [trade_date],
            ).fetchone()[0])
            # A known total with missing codes is normally a page-boundary
            # problem.  Reconcile once in reverse board-code order and persist
            # only codes absent from the primary sweep.  This stays bounded to
            # at most two small page sweeps and avoids one request per board.
            if em_total and 0 < em_rows_now < em_total:
                reverse_limit = min(max_pages, max(1, expected_pages))
                for reverse_page in range(1, reverse_limit + 1):
                    reverse_raw = None
                    reverse_error = ""
                    for attempt in range(2):
                        try:
                            shared_host_limiter.acquire("eastmoney", max(float(pause_seconds), 1.0))
                            reverse_raw, _ = _from_em_sector_flow_page(
                                page=reverse_page,
                                page_size=page_size,
                                return_meta=True,
                                sort_field="f12",
                                sort_order="1",
                            )
                            break
                        except Exception as exc:
                            reverse_error = str(exc)[:500]
                            time.sleep(min(5.0, 1.5 * (attempt + 1)))
                    if reverse_raw is None:
                        error = (error + "; " if error else "") + (
                            "reverse reconciliation: " + (reverse_error or "empty response")
                        )
                        break
                    if not reverse_raw:
                        break
                    existing_codes = {
                        str(row[0]) for row in con.execute(
                            "SELECT DISTINCT sector_code FROM multi_source_sector_flow "
                            "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE "
                            "AND provider='eastmoney_sector_full'",
                            [trade_date],
                        ).fetchall()
                    }
                    missing_rows = [
                        row for row in _valid_rows(reverse_raw, trade_date)
                        if str(row.get("sector_code") or "") not in existing_codes
                    ]
                    if missing_rows:
                        store.store(
                            "sector_flow",
                            None,
                            missing_rows,
                            {
                                "source": "eastmoney_sector_full",
                                "status": "live",
                                "trade_date": trade_date,
                                "purpose": "reverse_code_reconciliation",
                            },
                            asset_type="sector",
                            trade_date=trade_date,
                        )
                    reconciliation_pages += 1
                    em_rows_now = int(con.execute(
                        "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                        "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE "
                        "AND provider='eastmoney_sector_full'",
                        [trade_date],
                    ).fetchone()[0])
                    if em_rows_now >= em_total:
                        break
                    if pause_seconds > 0:
                        time.sleep(float(pause_seconds))
                fetched_rows = int(con.execute(
                    "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                    "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE "
                    "AND provider IN ('eastmoney_sector_full','tushare_sector_full','derived_ths_stock_aggregate')",
                    [trade_date],
                ).fetchone()[0])
            if em_rows_now == 0:
                try:
                    data, meta = resilient_sources.get(
                        "sector_flow", None, ttl=0, date=trade_date,
                        top_n=max(300, expected_rows or page_size),
                    )
                    rows = _valid_rows(data, trade_date)
                    if rows and str(meta.get("status")) in {"live", "refreshed"}:
                        stored = store.store(
                            "sector_flow", None, rows,
                            {**meta, "source": "tushare_sector_full", "trade_date": trade_date},
                            asset_type="sector", trade_date=trade_date,
                        )
                        fetched_rows = int(con.execute(
                            "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                            "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE AND provider='tushare_sector_full'",
                            [trade_date],
                        ).fetchone()[0])
                        fetched_pages = 1 if stored.get("rows_written") else 0
                        expected_pages = max(expected_pages, 1)
                        provider_used = "tushare_sector_full" if fetched_rows else provider_used
                except Exception as exc:
                    error = (error + "; " if error else "") + str(exc)[:400]

            # Always retain the project's verified THS concept universe in
            # parallel with the industry/board provider.  The two code spaces
            # are not interchangeable; a 1022-row industry response must not
            # silently replace the 361 THS concept flows.
            derived_rows = _aggregate_ths_stock_flow(con, trade_date)
            if derived_rows:
                con.execute(
                    "DELETE FROM multi_source_sector_flow "
                    "WHERE source_date=CAST(? AS DATE) AND provider='derived_ths_stock_aggregate'",
                    [trade_date],
                )
                store.store(
                    "sector_flow", None, derived_rows,
                    {"source": "derived_ths_stock_aggregate", "status": "live", "trade_date": trade_date},
                    asset_type="sector", trade_date=trade_date,
                )
                fetched_rows = int(con.execute(
                    "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
                    "WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE AND provider IN ('eastmoney_sector_full','tushare_sector_full','derived_ths_stock_aggregate')",
                    [trade_date],
                ).fetchone()[0])
                fetched_pages = max(fetched_pages, 1)
                expected_pages = max(expected_pages, 1)
                provider_used = "+".join(dict.fromkeys(filter(None, (provider_used, "derived_ths_stock_aggregate"))))

            ths_snap, ths_age = _ths_membership_snapshot(con, trade_date)
            ths_members_stale = ths_age is not None and ths_age > THS_MEMBERSHIP_MAX_AGE_DAYS
            ths_members_partial = False
            ths_members_partial_count = 0
            if table_exists(con, "ths_concept_member_checkpoint") and ths_snap:
                try:
                    # success_stale is a COMPLETE prior weekly snapshot reused
                    # when live THS is blocked; only its source date is old.
                    # It must not degrade the taxonomy to partial_members --
                    # that single page was the cause of the 8/3-8/10 intraday
                    # sector-flow runs all failing (30 consecutive degraded
                    # steps) while every taxonomy was actually 100% fetched.
                    partial_row = con.execute(
                        "SELECT count(*) FROM ths_concept_member_checkpoint "
                        "WHERE trade_date=CAST(? AS DATE) "
                        "AND status IN ('error','partial','empty','running')",
                        [ths_snap],
                    ).fetchone()
                    ths_members_partial_count = int(partial_row[0] or 0)
                    ths_members_partial = ths_members_partial_count > 0
                except Exception:
                    ths_members_partial = False

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
                # A4: a stale THS membership snapshot must not be certified as a
                # fully-ready concept taxonomy, even at full coverage.
                if taxonomy == "ths_concept" and ths_members_stale:
                    tax_status = "stale_members"
                    tax_error = (
                        f"ths membership snapshot {ths_snap} is {ths_age}d old "
                        f"(> {THS_MEMBERSHIP_MAX_AGE_DAYS}d)"
                    )
                elif taxonomy == "ths_concept" and ths_members_partial:
                    tax_status = "partial_members"
                    tax_error = f"{ths_members_partial_count} THS member pages are partial/error"
                else:
                    tax_error = "unverified denominator" if unverified else ""
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
            store.sync_sector_capital(trade_date)
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
    result = (
        rebuild_ths_derived_flow(args.db, args.date)
        if args.ths_derived_only
        else collect_full_sector_flow(
            args.db, args.date, page_size=args.page_size,
            max_pages=args.max_pages, pause_seconds=args.pause_seconds,
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
