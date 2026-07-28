"""Daily operator review report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_columns, table_exists
from trade_system.reports.real_data_backfill import build_real_data_backfill_status
from trade_system.review_statistics import build_daily_review_statistics


def _fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _rows(con: duckdb.DuckDBPyConnection, table: str, sql: str, params: list[Any]) -> list[dict]:
    if not table_exists(con, table):
        return []
    try:
        return _fetch_dicts(con, sql, params)
    except Exception:
        return []


def _latest_date(con: duckdb.DuckDBPyConnection) -> str:
    for table in ("market_regime_snapshot", "stock_candidate_stage_signal", "stock_candidate_score"):
        if table_exists(con, table):
            row = con.execute(f"SELECT max(trade_date) FROM {table}").fetchone()
            if row and row[0]:
                return str(row[0])
    return ""


def _capital_flow_review(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """Build the explicit daily fund-flow review contract.

    Rows are deduplicated by asset code before ranking.  Sector rankings are
    kept in one taxonomy (THS concepts when available, otherwise the clearly
    labelled derived concept fallback) instead of mixing DC industries and
    concept aggregates with incompatible semantics.
    """
    result: dict[str, Any] = {
        "stock_inflow": [], "stock_outflow": [], "sector_inflow": [],
        "sector_outflow": [], "industry_inflow": [], "industry_outflow": [],
        "sector_limit_up": [], "stock_flow_meta": {},
        "sector_flow_meta": {}, "candidate_picks": [],
    }
    if table_exists(con, "multi_source_stock_flow"):
        result["stock_flow_meta"] = _fetch_dicts(
            con,
            """
            SELECT count(*) AS rows, count(DISTINCT stock_code) AS codes,
                   max(fetched_at) AS fetched_at,
                   coalesce((SELECT status FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)), 'unknown') AS batch_status,
                   coalesce((SELECT coverage_pct FROM intraday_stock_flow_batch WHERE trade_date=CAST(? AS DATE)), NULL) AS batch_coverage_pct,
                   string_agg(DISTINCT coalesce(provider, 'unknown'), ', ' ORDER BY coalesce(provider, 'unknown')) AS providers
            FROM multi_source_stock_flow
            WHERE source_date=CAST(? AS DATE) AND coalesce(is_stale,FALSE)=FALSE
            """,
            [trade_date, trade_date, trade_date],
        )[0]
        stock_sql = """
            WITH ranked AS (
                SELECT f.*, coalesce(json_extract_string(f.raw_json, '$.name'), b.stock_name, '') AS stock_name,
                       row_number() OVER (
                         PARTITION BY f.stock_code
                          ORDER BY CASE f.provider
                                     WHEN 'eastmoney_market' THEN 1
                                     WHEN 'eastmoney_intraday_clist_delay' THEN 2
                                     WHEN 'eastmoney_intraday_clist' THEN 3
                                     WHEN 'tushare' THEN 4
                                     WHEN 'tushare_relay' THEN 4
                                     WHEN 'kpl' THEN 5
                                     ELSE 9
                                   END,
                                  f.fetched_at DESC NULLS LAST
                       ) AS provider_rank
                FROM multi_source_stock_flow f
                LEFT JOIN tushare_stock_basic b ON b.stock_code=f.stock_code
                WHERE f.source_date=CAST(? AS DATE) AND coalesce(f.is_stale,FALSE)=FALSE
            ), deduped AS (SELECT * FROM ranked WHERE provider_rank=1)
            SELECT stock_code, stock_name, main_net, super_net, large_net, close,
                   change_pct, turnover, provider, fetched_at
            FROM deduped ORDER BY main_net {direction} NULLS LAST LIMIT 50
        """
        try:
            result["stock_inflow"] = _fetch_dicts(con, stock_sql.format(direction="DESC"), [trade_date])
            result["stock_outflow"] = _fetch_dicts(con, stock_sql.format(direction="ASC"), [trade_date])
        except Exception:
            pass

    if table_exists(con, "multi_source_sector_flow"):
        result["sector_flow_meta"] = _fetch_dicts(
            con,
            """
            SELECT count(*) AS rows, count(DISTINCT sector_code) AS codes,
                   max(fetched_at) AS fetched_at,
                   coalesce((SELECT status FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)), 'unknown') AS batch_status,
                   coalesce((SELECT coverage_pct FROM intraday_sector_flow_batch WHERE trade_date=CAST(? AS DATE)), NULL) AS batch_coverage_pct,
                   string_agg(DISTINCT coalesce(sector_type,'unknown'), ', ' ORDER BY coalesce(sector_type,'unknown')) AS taxonomy
            FROM multi_source_sector_flow
            WHERE source_date=CAST(? AS DATE) AND coalesce(is_stale,FALSE)=FALSE
            """,
            [trade_date, trade_date, trade_date],
        )[0]
        sector_sql = """
            SELECT sector_code, sector_name, sector_type, main_net, change_pct,
                   provider, fetched_at
            FROM multi_source_sector_flow
            WHERE source_date=CAST(? AS DATE)
              AND coalesce(is_stale,FALSE)=FALSE
              AND sector_type IN ('ths_concept','ths_concept_derived')
            ORDER BY main_net {direction} NULLS LAST LIMIT 10
        """
        try:
            result["sector_inflow"] = _fetch_dicts(con, sector_sql.format(direction="DESC"), [trade_date])
            result["sector_outflow"] = _fetch_dicts(con, sector_sql.format(direction="ASC"), [trade_date])
            industry_sql = sector_sql.replace(
                "sector_type IN ('ths_concept','ths_concept_derived')",
                "sector_type = 'em_industry'",
            )
            result["industry_inflow"] = _fetch_dicts(con, industry_sql.format(direction="DESC"), [trade_date])
            result["industry_outflow"] = _fetch_dicts(con, industry_sql.format(direction="ASC"), [trade_date])
        except Exception:
            pass

    if table_exists(con, "ths_concept_stock_history") and table_exists(con, "v_limit_pool"):
        result["sector_limit_up"] = _rows(
            con,
            "ths_concept_stock_history",
            """
            SELECT h.concept_code AS sector_code, max(h.concept_name) AS sector_name,
                   count(DISTINCT l.stock_code) AS limit_up_count,
                   string_agg(DISTINCT coalesce(l.stock_name,h.stock_name), ', ' ORDER BY coalesce(l.stock_name,h.stock_name)) AS limit_up_stocks
            FROM ths_concept_stock_history h
            JOIN v_limit_pool l
              ON l.stock_code=regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '')
             AND l.trade_date=?
            WHERE h.trade_date=(
                SELECT max(trade_date) FROM ths_concept_stock_history
                WHERE trade_date<=CAST(? AS DATE)
            )
            GROUP BY h.concept_code
            ORDER BY limit_up_count DESC, sector_name
            """,
            [trade_date, trade_date],
        )

    if table_exists(con, "stock_candidate_score"):
        result["candidate_picks"] = _rows(
            con,
            "stock_candidate_score",
            """
            WITH provider_ranked AS (
                SELECT stock_code, main_net,
                       row_number() OVER (
                         PARTITION BY stock_code ORDER BY CASE provider
                           WHEN 'eastmoney_market' THEN 1
                           WHEN 'eastmoney_intraday_clist_delay' THEN 2
                           WHEN 'eastmoney_intraday_clist' THEN 3
                           WHEN 'tushare' THEN 4
                           WHEN 'tushare_relay' THEN 4
                           WHEN 'kpl' THEN 5
                           ELSE 9 END,
                         fetched_at DESC NULLS LAST
                       ) AS provider_rank
                FROM multi_source_stock_flow
                WHERE source_date=CAST(? AS DATE) AND coalesce(is_stale,FALSE)=FALSE
            ), flow AS (
                SELECT stock_code,main_net,
                       row_number() OVER (
                         ORDER BY main_net DESC NULLS LAST,stock_code
                       ) AS flow_rank
                FROM provider_ranked
                WHERE provider_rank=1
            )
            SELECT s.stock_code, s.stock_name, s.score, s.source, s.sector_code,
                   f.main_net, f.flow_rank,
                   CASE WHEN s.source='limit_pool' THEN 'research_only_limit_pool' ELSE 'research_only' END AS selection_status
            FROM stock_candidate_score s
            LEFT JOIN flow f ON f.stock_code=s.stock_code
            WHERE s.trade_date=?
            ORDER BY s.score DESC NULLS LAST, f.main_net DESC NULLS LAST
            LIMIT 20
            """,
            [trade_date, trade_date],
        )
    return result


def build_daily_review_context(db_path: str | Path, trade_date: str | None = None) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        selected_date = trade_date or _latest_date(con)
        regime_order = "generated_at DESC NULLS LAST" if "generated_at" in table_columns(con, "market_regime_snapshot") else "trade_date DESC"
        regime = _rows(
            con,
            "market_regime_snapshot",
            f"SELECT * FROM market_regime_snapshot WHERE trade_date = ? ORDER BY {regime_order} LIMIT 1",
            [selected_date],
        )
        sectors = _rows(
            con,
            "sector_rotation_score",
            "SELECT * FROM sector_rotation_score WHERE trade_date = ? ORDER BY score DESC LIMIT 10",
            [selected_date],
        )
        stages = _rows(
            con,
            "stock_candidate_stage_signal",
            """
            SELECT stage, stock_code, stock_name, score, decision
            FROM stock_candidate_stage_signal
            WHERE trade_date = ?
            ORDER BY stage, score DESC NULLS LAST, stock_code
            LIMIT 80
            """,
            [selected_date],
        )
        alerts = _rows(
            con,
            "alert_events",
            "SELECT severity, category, message FROM alert_events WHERE trade_date = ? ORDER BY severity, category",
            [selected_date],
        )
        watchlist = _rows(
            con,
            "watchlist",
            "SELECT stock_code, stock_name, sector_code, thesis, invalidation, priority, status FROM watchlist WHERE trade_date = ? ORDER BY priority",
            [selected_date],
        )
        plans = _rows(
            con,
            "trade_plan",
            "SELECT stock_code, stock_name, setup_type, max_position_pct, status, entry_condition, stop_condition FROM trade_plan WHERE trade_date = ? ORDER BY max_position_pct DESC",
            [selected_date],
        )
        risk = _rows(
            con,
            "risk_snapshot",
            "SELECT risk_state, total_position_pct, max_single_position_pct, max_sector_position_pct, evidence_json FROM risk_snapshot WHERE trade_date = ? ORDER BY created_at DESC LIMIT 1",
            [selected_date],
        )
        journal = _rows(
            con,
            "trade_journal",
            "SELECT stock_code, stock_name, action, action_time, reason, mistake_tag FROM trade_journal WHERE trade_date = ? ORDER BY action_time, stock_code",
            [selected_date],
        )
        outcomes = _rows(
            con,
            "operator_trade_outcome",
            """
            SELECT stock_code, stock_name, execution_status, position_pct, gross_return_pct,
                   net_return_pct, outcome_tag, mistake_tag, review_note
            FROM operator_trade_outcome
            WHERE trade_date = ?
            ORDER BY stock_code
            """,
            [selected_date],
        )
        capital_flow = _capital_flow_review(con, selected_date)
    finally:
        con.close()

    return {
        "trade_date": selected_date,
        "regime": regime[0] if regime else {},
        "sectors": sectors,
        "stages": stages,
        "alerts": alerts,
        "watchlist": watchlist,
        "plans": plans,
        "risk": risk[0] if risk else {},
        "journal": journal,
        "outcomes": outcomes,
        "capital_flow": capital_flow,
        "statistics": build_daily_review_statistics(db_path),
        "backfill": build_real_data_backfill_status(db_path),
    }


def _table_rows(rows: list[dict], columns: list[str], empty_text: str = "No rows") -> list[str]:
    if not rows:
        return [f"| {empty_text} |" + " |" * (len(columns) - 1)]
    output = []
    for row in rows:
        output.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return output


def _render_flow_review_sections(flow: dict[str, Any]) -> list[str]:
    """Render the explicit fund-flow and research-pick contract.

    These sections deliberately distinguish research candidates from executable
    orders.  A daily review must still be useful when one provider is stale or
    a required execution gate (K-line, sector, auction, or outcome) is absent.
    """
    stock_meta = flow.get("stock_flow_meta", {})
    sector_meta = flow.get("sector_flow_meta", {})
    sector_title = "THS Concept" if "ths" in str(sector_meta.get("taxonomy") or "").lower() else "Sector"
    sector_status = sector_meta.get("batch_status") or "unknown"
    sector_coverage = sector_meta.get("batch_coverage_pct")
    lines = [
        "## Capital Flow Coverage",
        "",
        f"- Stock flow: `{stock_meta.get('codes', 0)}` codes / `{stock_meta.get('rows', 0)}` rows; providers `{stock_meta.get('providers', '')}`; batch `{stock_meta.get('batch_status', 'unknown')}` ({stock_meta.get('batch_coverage_pct', '')}%); fetched `{stock_meta.get('fetched_at', '')}`.",
        f"- Sector flow: `{sector_meta.get('codes', 0)}` concepts / `{sector_meta.get('rows', 0)}` rows; taxonomy `{sector_meta.get('taxonomy', '')}`; batch `{sector_status}` ({sector_coverage if sector_coverage is not None else ''}%); fetched `{sector_meta.get('fetched_at', '')}`.",
        "",
        "### Individual Stock Main-Net Inflow Top 50",
        "",
        "| Code | Name | Main Net | Super | Large | Change % | Provider |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    lines.extend(_table_rows(flow.get("stock_inflow", []), ["stock_code", "stock_name", "main_net", "super_net", "large_net", "change_pct", "provider"], "No stock inflow rows"))
    lines.extend(
        [
            "",
            "### Individual Stock Main-Net Outflow Top 50",
            "",
            "| Code | Name | Main Net | Super | Large | Change % | Provider |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("stock_outflow", []), ["stock_code", "stock_name", "main_net", "super_net", "large_net", "change_pct", "provider"], "No stock outflow rows"))
    lines.extend(
        [
            "",
            f"### THS Concept Flow Inflow Top 10 ({sector_title} rows)",
            "",
            "| Concept | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("sector_inflow", []), ["sector_name", "main_net", "change_pct", "provider"], f"No {sector_title.lower()} inflow rows"))
    lines.extend(
        [
            "",
            f"### THS Concept Flow Outflow Top 10 ({sector_title} rows)",
            "",
            "| Concept | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("sector_outflow", []), ["sector_name", "main_net", "change_pct", "provider"], f"No {sector_title.lower()} outflow rows"))
    lines.extend(
        [
            "",
            "### Eastmoney Industry Flow Inflow Top 10",
            "",
            "| Industry | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("industry_inflow", []), ["sector_name", "main_net", "change_pct", "provider"], "No industry inflow rows"))
    lines.extend(
        [
            "",
            "### Eastmoney Industry Flow Outflow Top 10",
            "",
            "| Industry | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("industry_outflow", []), ["sector_name", "main_net", "change_pct", "provider"], "No industry outflow rows"))
    lines.extend(
        [
            "",
            "### THS Concepts With Limit-Up Stocks",
            "",
            "| Concept | Limit-Up Count | Stocks |",
            "|---|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("sector_limit_up", []), ["sector_name", "limit_up_count", "limit_up_stocks"], "No concept limit-up mapping rows"))
    lines.extend(
        [
            "",
            "### Potential Stocks (Research Only)",
            "",
            "These are review candidates, not executable orders. Promote only after K-line, sector, auction, readiness, and manual-risk gates pass.",
            "",
            "| Code | Name | Score | Main Net | Flow Rank | Selection Status |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(flow.get("candidate_picks", []), ["stock_code", "stock_name", "score", "main_net", "flow_rank", "selection_status"], "No research candidates"))
    return lines


def render_daily_review_markdown(context: dict) -> str:
    regime = context.get("regime", {})
    risk = context.get("risk", {})
    stats = context.get("statistics", {})
    backfill = context.get("backfill", {})
    flow = context.get("capital_flow", {})
    stock_meta = flow.get("stock_flow_meta", {})
    sector_meta = flow.get("sector_flow_meta", {})
    lines = [
        f"# Daily Review - {context.get('trade_date', '')}",
        "",
        "## Market Regime",
        "",
        f"- Regime: `{regime.get('regime', 'unknown')}`",
        f"- Regime score: `{regime.get('regime_score', '')}`",
        f"- Suggested position: `{regime.get('suggested_position_pct', '')}%`",
        f"- Risk state: `{risk.get('risk_state', 'unknown')}`",
    ]
    lines.extend([""] + _render_flow_review_sections(flow))
    lines.extend(["", "## Mainline Themes", "", "| Sector | Score | Strength | Limit Up |", "|---|---:|---:|---:|"])
    lines.extend(_table_rows(context.get("sectors", []), ["sector_name", "score", "strength_value", "limit_up_count"]))

    lines.extend(
        [
            "",
            "## Four-Stage Candidates",
            "",
            "| Stage | Stock | Score | Decision |",
            "|---|---|---:|---|",
        ]
    )
    stage_rows = [
        {
            "stage": row.get("stage"),
            "stock": row.get("stock_name") or row.get("stock_code"),
            "score": row.get("score"),
            "decision": row.get("decision"),
        }
        for row in context.get("stages", [])
    ]
    lines.extend(_table_rows(stage_rows, ["stage", "stock", "score", "decision"]))

    lines.extend(["", "## Risk Alerts", "", "| Severity | Category | Message |", "|---|---|---|"])
    lines.extend(_table_rows(context.get("alerts", []), ["severity", "category", "message"]))

    lines.extend(["", "## Plan Execution", "", "| Stock | Max Position | Status | Entry | Stop |", "|---|---:|---|---|---|"])
    plan_rows = [
        {
            "stock": row.get("stock_name") or row.get("stock_code"),
            "max_position_pct": row.get("max_position_pct"),
            "status": row.get("status"),
            "entry_condition": row.get("entry_condition"),
            "stop_condition": row.get("stop_condition"),
        }
        for row in context.get("plans", [])
    ]
    lines.extend(_table_rows(plan_rows, ["stock", "max_position_pct", "status", "entry_condition", "stop_condition"]))

    lines.extend(["", "## Mistakes And Invalidations", "", "| Stock | Action | Reason | Tag |", "|---|---|---|---|"])
    review_rows = [
        {
            "stock": row.get("stock_name") or row.get("stock_code"),
            "action": row.get("action"),
            "reason": row.get("reason"),
            "mistake_tag": row.get("mistake_tag"),
        }
        for row in context.get("journal", [])
        if row.get("mistake_tag") not in ("", None, "none")
    ]
    lines.extend(_table_rows(review_rows, ["stock", "action", "reason", "mistake_tag"]))

    lines.extend(
        [
            "",
            "## Outcome Review",
            "",
            "| Stock | Status | Position | Gross % | Net % | Outcome | Mistake | Note |",
            "|---|---|---:|---:|---:|---|---|---|",
        ]
    )
    outcome_rows = [
        {
            "stock": row.get("stock_name") or row.get("stock_code"),
            "execution_status": row.get("execution_status"),
            "position_pct": row.get("position_pct"),
            "gross_return_pct": row.get("gross_return_pct"),
            "net_return_pct": row.get("net_return_pct"),
            "outcome_tag": row.get("outcome_tag"),
            "mistake_tag": row.get("mistake_tag"),
            "review_note": row.get("review_note"),
        }
        for row in context.get("outcomes", [])
    ]
    lines.extend(
        _table_rows(
            outcome_rows,
            [
                "stock",
                "execution_status",
                "position_pct",
                "gross_return_pct",
                "net_return_pct",
                "outcome_tag",
                "mistake_tag",
                "review_note",
            ],
            "No imported operator outcomes",
        )
    )

    lines.extend(
        [
            "",
            "## Next-Day Focus",
            "",
            "- Recheck top mainline themes before auction.",
            "- Keep candidates only if auction and intraday evidence confirm the thesis.",
            "- Respect risk_snapshot max position before any manual action.",
            "",
            "## Data Gaps And Degradation",
            "",
        ]
    )
    gaps = backfill.get("gaps", [])
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps[:20])
    else:
        lines.append("- No critical gaps detected.")

    lines.extend(
        [
            "",
            "## Stage Validation",
            "",
            "| Stage | Return Samples | Verdict |",
            "|---|---:|---|",
        ]
    )
    for stage, item in sorted(stats.get("stage_statistics", {}).items()):
        lines.append(f"| {stage} | {item.get('return_sample_count', 0)} | `{item.get('verdict')}` |")
    lines.append("")
    return "\n".join(lines)


def write_daily_review(db_path: str | Path, out_path: str | Path, trade_date: str | None = None) -> Path:
    context = build_daily_review_context(db_path, trade_date)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_daily_review_markdown(context), encoding="utf-8")
    return path
