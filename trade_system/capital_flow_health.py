"""Focused health checks for stock and sector capital-flow evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from base import connect_duckdb
from trade_system.quality import table_columns, table_exists


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
) -> dict:
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
        "(" + " OR ".join(f'"{name}" IS NOT NULL' for name in flow_columns if name in columns) + ")"
        if any(name in columns for name in flow_columns)
        else "TRUE"
    )
    if relation == "multi_source_stock_flow" and "main_net" in columns:
        valid_flow = '"main_net" IS NOT NULL'
    date_filter = f'"{date_column}" = CAST(? AS DATE) AND {valid_flow}'
    rows, codes, latest, latest_source_time = con.execute(
        f"""
        SELECT count(*), {code_expr}, {timestamp_expr}, {source_time_expr}
        FROM "{relation}"
        WHERE {date_filter}
        """,
        [trade_date],
    ).fetchone()
    recent_rows = int(rows or 0)
    recent_codes = int(codes or 0)
    if collected_after is not None or max_age_seconds is not None:
        if timestamp_column:
            freshness_filters = []
            freshness_params = [trade_date]
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
            observed_at = latest if isinstance(latest, datetime) else datetime.fromisoformat(str(latest))
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
    min_coverage_pct: float = 80.0,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> dict:
    if isinstance(collected_after, str):
        collected_after = datetime.fromisoformat(collected_after)
    con = connect_duckdb(str(db_path), read_only=True)
    try:
        # Full-market collectors persist their expected universe in a durable
        # checkpoint.  Use it automatically when the CLI caller does not
        # provide a count; otherwise a partial snapshot can look ready merely
        # because it contains some rows.
        if not expected_stock_codes and table_exists(con, "intraday_stock_flow_batch"):
            batch_row = con.execute(
                "SELECT coalesce(expected_rows,0) FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
            expected_stock_codes = int(batch_row[0] or 0) if batch_row else 0
        if not expected_sector_codes and table_exists(con, "intraday_sector_flow_batch"):
            batch_row = con.execute(
                "SELECT coalesce(expected_rows,0) FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)",
                [trade_date],
            ).fetchone()
            expected_sector_codes = int(batch_row[0] or 0) if batch_row else 0
        stock_relations = [
            _relation_health(
                con, relation, trade_date, "stock_code", collected_after,
                max_age_seconds=max_age_seconds, now=now,
            )
            for relation in STOCK_FLOW_RELATIONS
        ]
        sector_relations = [
            _relation_health(
                con, relation, trade_date, "sector_code", collected_after,
                max_age_seconds=max_age_seconds, now=now,
            )
            for relation in SECTOR_FLOW_RELATIONS
        ]
    finally:
        con.close()

    gated = collected_after is not None or max_age_seconds is not None
    code_field = "recent_codes" if gated else "codes"
    row_field = "recent_rows" if gated else "rows"
    stock_codes = max((item[code_field] for item in stock_relations), default=0)
    stock_coverage = (
        stock_codes * 100.0 / expected_stock_codes if expected_stock_codes else None
    )
    stock_ready = any(item[row_field] > 0 for item in stock_relations) and (
        stock_coverage is None or stock_coverage >= min_coverage_pct
    )
    # Sector capital is the core directional flow; intraday sector volume alone is not equivalent.
    sector_candidates = [
        item for item in sector_relations
        if item["relation"] in {"multi_source_sector_flow", "sector_capital"}
    ]
    sector_flow = max(sector_candidates, key=lambda item: item[code_field], default={"rows": 0, "codes": 0, "recent_rows": 0, "recent_codes": 0})
    sector_codes = max((item[code_field] for item in sector_candidates), default=0)
    sector_coverage = (
        sector_codes * 100.0 / expected_sector_codes if expected_sector_codes else None
    )
    sector_ready = sector_flow[row_field] > 0 and (
        sector_coverage is None or sector_coverage >= min_coverage_pct
    )
    # A full-sector checkpoint is authoritative when present.  Do not let a
    # smaller bounded/legacy relation make a partial batch look ready.
    try:
        con = connect_duckdb(str(db_path), read_only=True)
        sector_batch = con.execute(
            "SELECT status,coverage_pct FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)",
            [trade_date],
        ).fetchone() if table_exists(con, "intraday_sector_flow_batch") else None
        con.close()
    except Exception:
        sector_batch = None
    # A full-sector run may be marked ``partial`` when an optional taxonomy
    # provider (for example TuShare DC) is unavailable even though the
    # authoritative Eastmoney/THS universe is complete.  Coverage is the
    # readiness gate; preserve the provider status in the report without
    # turning a complete 870/870 snapshot into a hard failure.
    if sector_batch:
        batch_status = str(sector_batch[0] or "").lower()
        batch_coverage = float(sector_batch[1] or 0)
        batch_usable = batch_status in {
            "success",
            "partial",
            "success_with_unavailable",
            "success_with_optional_gap",
        }
        if not batch_usable or batch_coverage < min_coverage_pct:
            sector_ready = False
    # A4: surface stale THS concept membership.  The concept taxonomy can be
    # coverage-complete yet built on an out-of-date membership snapshot; that must
    # be visible to the gate/report even though directional industry flow is fine.
    sector_taxonomy_stale = False
    sector_taxonomy_note = ""
    try:
        con = connect_duckdb(str(db_path), read_only=True)
        if table_exists(con, "intraday_sector_flow_taxonomy"):
            stale_row = con.execute(
                "SELECT status, last_error FROM intraday_sector_flow_taxonomy "
                "WHERE trade_date=CAST(? AS DATE) AND taxonomy='ths_concept'",
                [trade_date],
            ).fetchone()
            if stale_row and str(stale_row[0] or "").lower() == "stale_members":
                sector_taxonomy_stale = True
                sector_taxonomy_note = str(stale_row[1] or "ths concept membership is stale")
        con.close()
    except Exception:
        sector_taxonomy_stale = False
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "collection_started_at": collected_after.isoformat(timespec="seconds")
        if collected_after is not None
        else None,
        "min_coverage_pct": float(min_coverage_pct),
        "max_age_seconds": int(max_age_seconds) if max_age_seconds is not None else None,
        "ready": stock_ready and sector_ready,
        "stock_flow": {
            "ready": stock_ready,
            "observed_codes": stock_codes,
            "expected_codes": int(expected_stock_codes or 0),
            "coverage_pct": round(stock_coverage, 2) if stock_coverage is not None else None,
            "relations": stock_relations,
        },
        "sector_flow": {
            "ready": sector_ready,
            "observed_codes": sector_codes,
            "expected_codes": int(expected_sector_codes or 0),
            "coverage_pct": round(sector_coverage, 2) if sector_coverage is not None else None,
            "relations": sector_relations,
            "taxonomy_stale": sector_taxonomy_stale,
            "taxonomy_stale_note": sector_taxonomy_note,
        },
    }


def render_capital_flow_health_markdown(result: dict) -> str:
    lines = [
        "# Capital Flow Freshness",
        "",
        f"- Generated at: `{result['generated_at']}`",
        f"- Trade date: `{result['trade_date']}`",
        f"- Overall ready: `{str(result['ready']).lower()}`",
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
            f"- Minimum required: `{result['min_coverage_pct']}%` when an expected universe is supplied",
            f"- Stock codes: `{result['stock_flow']['observed_codes']}` / "
            f"`{result['stock_flow']['expected_codes'] or 'not supplied'}`",
            f"- Sector codes: `{result['sector_flow']['observed_codes']}` / "
            f"`{result['sector_flow']['expected_codes'] or 'not supplied'}`",
            "",
        ]
    )
    if result["sector_flow"].get("taxonomy_stale"):
        lines.append(
            f"- WARNING: THS concept membership stale -- "
            f"`{result['sector_flow'].get('taxonomy_stale_note', '')}` "
            "(concept taxonomy is not certified ready)"
        )
    return "\n".join(lines)
