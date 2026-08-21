"""Daily operator review report."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from trade_system.quality import table_columns, table_exists
from trade_system.capital_flow_health import assess_capital_flow_health
from trade_system.gate_contract import build_operator_state
from trade_system.logging_setup import get_logger
from trade_system.readiness import assess_trade_date_readiness
from trade_system.reports.real_data_backfill import build_real_data_backfill_status
from trade_system.review_statistics import build_daily_review_statistics

logger = get_logger(__name__)

# Facade re-exports: the implementations moved to review_format /
# review_queries; every historical private name stays importable from here.
from trade_system.review_format import (  # noqa: F401
    _fmt_money,
    _fmt_pct,
    _format_flow_rows,
)
from trade_system.review_queries import (  # noqa: F401
    _rows,
    _latest_date,
    _capital_flow_review,
    _concept_limit_up_review,
    _market_context_review,
    _data_source_review,
    _regime_key,
    _ecology_review,
    _derived_limit_board_levels,
    _sector_trail_review,
    _period_label,
    _compound_pct,
    _period_frames,
    _sector_period_review,
    _REGIME_NOTES,
    _REGIME_FORECAST,
)


def _cn_weekday(trade_date: str) -> str:
    try:
        day = datetime.strptime(str(trade_date)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return "星期" + "一二三四五六日"[day.weekday()]


def _breadth_snapshot(context: dict) -> dict[str, Any]:
    """Collapse multi-source breadth rows into one readable snapshot."""
    market = context.get("market_context") or {}
    breadth = market.get("breadth") or []
    row = breadth[0] if breadth else {}
    limit_up = row.get("limit_up_count")
    limit_down = row.get("limit_down_count")
    rise = fall = None
    for item in breadth:
        if item.get("rise_count") is not None:
            rise = item.get("rise_count")
        if item.get("fall_count") is not None:
            fall = item.get("fall_count")
    blown = None
    limit_summary = market.get("limit_summary") or []
    if limit_summary:
        blown = limit_summary[0].get("blown_limit_up_rate")
    return {
        "limit_up": limit_up,
        "limit_down": limit_down,
        "rise": rise,
        "fall": fall,
        "blown_rate": blown,
    }


def build_review_narrative(context: dict) -> dict[str, Any]:
    """Deterministic one-screen review: verdict, lede, bullets, next-day focus.

    Shared by the markdown archive and the HTML review page so the two
    surfaces cannot drift into different conclusions.
    """
    trade_date = str(context.get("trade_date") or "")
    regime = context.get("regime") or {}
    readiness = context.get("readiness") or {}
    control = context.get("execution_control") or {}
    risk = context.get("risk") or {}
    concepts = (context.get("concept_limit_up") or {}).get("groups") or []
    flow = context.get("capital_flow") or {}
    alerts = context.get("alerts") or []
    outcomes = context.get("outcomes") or []
    plans = context.get("plans") or []
    journal = context.get("journal") or []
    watchlist = context.get("watchlist") or []
    breadth = _breadth_snapshot(context)

    regime_name = str(regime.get("regime_name") or regime.get("regime") or "未知")
    top_concept = (concepts[0].get("concept_name") if concepts else None) or None
    top_limit_up = concepts[0].get("limit_up_count") if concepts else None
    missing = [str(item) for item in (readiness.get("missing_groups") or []) if item]
    blocked = control.get("override") == "BLOCK" or not readiness.get(
        "analysis_ready", readiness.get("certified_ready", False)
    )
    execution_ready = bool(control.get("execution_ready"))
    effective = control.get("effective_position_pct")
    if effective is None:
        effective = 0 if blocked else regime.get("suggested_position_pct")
    suggested = regime.get("suggested_position_pct")
    inflow = (flow.get("stock_inflow") or [None])[0]
    outflow = (flow.get("stock_outflow") or [None])[0]
    broad = {"融资融券", "沪股通", "深股通", "国企改革", "富时罗素", "标普道琼斯", "融资融券概念"}

    def _first_named(rows: list, name_key: str) -> dict | None:
        for row in rows or []:
            name = str(row.get(name_key) or "")
            if name and name not in broad:
                return row
        return None

    sector_in = _first_named(flow.get("sector_inflow") or [], "sector_name")
    sector_out = _first_named(flow.get("sector_outflow") or [], "sector_name")

    if blocked:
        stance = "blocked"
        stance_label = "仅可复盘"
        if missing:
            headline = f"{regime_name}格局，{top_concept or '主线不明'}，但{missing[0]}未齐"
        else:
            headline = f"{regime_name}格局，数据门禁未开放"
        lede = "收盘源或链路尚未完成。本页只作复盘，不能把候选、分数或影子模型当成开仓依据。"
    elif not execution_ready:
        stance = "observe"
        stance_label = "观察核验"
        headline = f"{regime_name}格局，主线看{top_concept or '分散'}"
        lede = "复盘事实已形成，执行门禁未开。计划只作核验，仓位仍受风险限额约束。"
    else:
        stance = "ready"
        stance_label = "可核验计划"
        headline = f"{regime_name}格局，主线{top_concept or '分散'}，可人工核验计划"
        lede = "可进入人工交易计划核验，仍须遵守 T+1、涨跌停和风险限额。"

    bullets: list[str] = []
    lu, ld, rise, fall, blown = (
        breadth.get("limit_up"),
        breadth.get("limit_down"),
        breadth.get("rise"),
        breadth.get("fall"),
        breadth.get("blown_rate"),
    )
    if lu is not None or rise is not None:
        piece = []
        if lu is not None:
            piece.append(f"涨停 {lu} / 跌停 {ld if ld is not None else '—'}")
        if rise is not None and fall is not None:
            piece.append(f"上涨 {rise} / 下跌 {fall}")
        if blown is not None:
            try:
                piece.append(f"炸板率 {float(blown):.1f}%")
            except (TypeError, ValueError):
                piece.append(f"炸板率 {blown}")
        bullets.append("市场宽度：" + "，".join(piece) + "。")
    if top_concept:
        extra = f"，同日涨停 {top_limit_up} 只" if top_limit_up is not None else ""
        bullets.append(f"主线证据：{top_concept}{extra}。概念排序只作复盘，不等于买入名单。")
    else:
        bullets.append("主线证据：当日没有足够干净的概念—涨停映射，主线按分散处理。")
    if inflow and outflow:
        bullets.append(
            "资金确认：个股净流入首位 "
            f"{inflow.get('stock_name') or inflow.get('stock_code')}，"
            "净流出首位 "
            f"{outflow.get('stock_name') or outflow.get('stock_code')}。"
        )
    if sector_in or sector_out:
        bits = []
        if sector_in:
            bits.append(f"概念流入 {sector_in.get('sector_name')}")
        if sector_out:
            bits.append(f"流出 {sector_out.get('sector_name')}")
        bullets.append("板块资金：" + "，".join(bits) + "。")
    if alerts:
        first = alerts[0]
        bullets.append(
            f"风险告警 {len(alerts)} 条，首条 {first.get('severity') or ''} / "
            f"{first.get('category') or ''}：{first.get('message') or '—'}"
        )
    if outcomes:
        bullets.append(f"操作闭环：已导入 {len(outcomes)} 条成交/跳过记录，按真实结果复盘。")
    else:
        bullets.append("操作闭环：今日没有导入成交。没有成交就不能用候选分数冒充胜率。")
    if plans:
        bullets.append(f"计划 {len(plans)} 条，观察池 {len(watchlist)} 条，日志 {len(journal)} 条。")

    ecology = context.get("ecology") or {}
    yday = ecology.get("yday") or {}
    leader = ecology.get("leader") or {}
    groups = ecology.get("ladder_groups") or []
    if groups:
        top = groups[0]
        names = "、".join(str(n) for n in (top.get("names") or [])[:4])
        extra = f"等 {top.get('count')} 只" if (top.get("count") or 0) > 4 else ""
        bullets.append(f"连板梯队：最高 {top.get('height')} 板 {names}{extra}。")
    if yday.get("n"):
        avg = yday.get("avg_ret")
        pos = yday.get("pos_rate")
        first = yday.get("first_avg")
        multi = yday.get("multi_avg")
        bits = [f"昨日涨停 {int(yday.get('n') or 0)} 只今日平均 {_fmt_pct(avg) or avg}"]
        if pos is not None:
            bits.append(f"正收益 {_fmt_pct(pos) or pos}")
        if first is not None:
            bits.append(f"首板 {_fmt_pct(first) or first}")
        if multi is not None:
            bits.append(f"多板 {_fmt_pct(multi) or multi}")
        bullets.append("溢价：" + "，".join(str(x) for x in bits) + "。")
    broken = ecology.get("broken") or []
    if broken:
        names = "、".join(
            f"{row.get('stock_name') or row.get('stock_code')}"
            for row in broken[:4]
        )
        bullets.append(f"接力失败：{names}。这是今日亏钱效应的前排来源。")

    key = _regime_key(regime_name)
    note = _REGIME_NOTES.get(key) or ""
    forecast = _REGIME_FORECAST.get(key) or "观望"
    risks = []
    if key == "退潮":
        risks.append("退潮期严禁接力和反包。")
    if key == "高潮":
        risks.append("高潮期兑现后排，不新开仓。")
    if (breadth.get("blown_rate") or 0) and float(breadth.get("blown_rate") or 0) >= 40:
        risks.append("炸板率极高，严禁追高。")
    if (breadth.get("limit_down") or 0) and int(breadth.get("limit_down") or 0) >= 10:
        risks.append("跌停偏多，避开弱势股。")
    if yday.get("avg_ret") is not None and float(yday.get("avg_ret") or 0) < 1:
        risks.append("昨日涨停几乎没有溢价，打板环境差。")

    cap = effective if effective is not None else 0
    tomorrow: list[str] = []
    pos_line = f"仓位上限 {cap}%"
    if suggested is not None:
        pos_line += f"（理论 {suggested}%）"
    tomorrow.append(f"{pos_line} · 情绪预判 {forecast} · 风险 {risk.get('risk_state') or '未评估'}。")
    if note:
        tomorrow.append(note)
    tomorrow.extend(risks[:3])
    if missing:
        tomorrow.append(f"先补齐 {', '.join(missing[:4])}，再讨论执行。")
    if leader.get("stock_name") or top_concept:
        who = leader.get("stock_name") or top_concept
        tomorrow.append(f"竞价核验 {who} 是否还能封住溢价，不能封就降级观察。")
    else:
        tomorrow.append("竞价前重新确认主线，避免把过宽概念当成方向。")
    if not outcomes:
        tomorrow.append("收盘后导入真实成交/跳过/取消，否则次日复盘仍是候选清单。")

    return {
        "trade_date": trade_date,
        "weekday": _cn_weekday(trade_date),
        "stance": stance,
        "stance_label": stance_label,
        "headline": headline,
        "lede": lede,
        "regime": regime_name,
        "mainline": top_concept or "暂无可靠主线",
        "effective_position_pct": cap,
        "suggested_position_pct": suggested,
        "missing": missing,
        "bullets": bullets,
        "tomorrow": tomorrow,
        "breadth": breadth,
        "playbook": {"note": note, "forecast": forecast, "risks": risks},
        "plan_count": len(plans),
        "outcome_count": len(outcomes),
        "watch_count": len(watchlist),
        "alert_count": len(alerts),
    }


def build_daily_review_context(
    db_path: str | Path,
    trade_date: str | None = None,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict:
    owns_connection = con is None
    con = con or duckdb.connect(str(db_path), read_only=True)
    flow_evidence_present = False
    try:
        selected_date = trade_date or _latest_date(con)
        regime_order = "generated_at DESC NULLS LAST" if "generated_at" in table_columns(con, "market_regime_snapshot") else "trade_date DESC"
        regime = _rows(
            con,
            "market_regime_snapshot",
            f"SELECT * FROM market_regime_snapshot WHERE trade_date = ? ORDER BY {regime_order} LIMIT 1",
            [selected_date],
        )
        rotation_columns = set(table_columns(con, "sector_rotation_score"))
        rotation_taxonomy_filter = (
            "AND (taxonomy IN ('ths_concept', 'ths_concept_derived') "
            "OR (coalesce(taxonomy, 'unknown') = 'unknown' AND sector_code LIKE 'THS-%'))"
            if "taxonomy" in rotation_columns
            else "AND sector_code LIKE 'THS-%'"
        )
        sectors = _rows(
            con,
            "sector_rotation_score",
            "SELECT * FROM sector_rotation_score WHERE trade_date = ? "
            + rotation_taxonomy_filter
            + " ORDER BY score DESC LIMIT 10",
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
        concept_limit_up = _concept_limit_up_review(con, selected_date)
        market_context = _market_context_review(con, selected_date)
        data_sources = _data_source_review(con, selected_date)
        ecology = _ecology_review(con, selected_date)
        sector_trail = _sector_trail_review(con, selected_date)
        sector_periods = _sector_period_review(con, selected_date)
        flow_evidence_present = any(
            table_exists(con, relation)
            for relation in (
                "multi_source_stock_flow",
                "multi_source_sector_flow",
                "sector_capital",
            )
        )
        try:
            # A post-close review must not accept a stale intraday snapshot as
            # current close evidence.  Keep the same 2-hour freshness contract
            # used by the integrated close gate.
            readiness = assess_trade_date_readiness(
                con, selected_date, stage="postmarket", max_age_seconds=7200
            )
        except Exception as exc:
            readiness = {
                "trade_date": selected_date, "stage": "postmarket",
                "ready": False, "source_ready": False, "pipeline_ready": False,
                "artifact_current": False, "certified_ready": False,
                "analytics_ready": False, "execution_ready": False,
                "missing_groups": [f"readiness_error:{type(exc).__name__}"],
                "groups": [], "actionable_candidates": 0,
                "tradable_candidates": 0, "risk_approved_candidates": 0,
                "executable_candidates": 0,
            }
    finally:
        if owns_connection:
            con.close()

    # Merge the independent flow certification into the review's operator
    # state.  Missing reconciliation is a hard false, so the page cannot show
    # a misleading third "not assessed" ready state.
    flow_health: dict[str, Any] = {}
    if flow_evidence_present:
        try:
            flow_health = assess_capital_flow_health(
                db_path,
                selected_date,
                max_age_seconds=None,
            )
        except Exception as exc:
            flow_health = {
                "flow_certified_ready": False,
                "warnings": [f"capital_flow_health_error:{type(exc).__name__}"],
            }
    flow_certified = bool(flow_health.get("flow_certified_ready", False)) if flow_health else False
    operator_state = build_operator_state(
        source_ready=bool(readiness.get("source_ready", readiness.get("ready", False))),
        pipeline_ready=bool(readiness.get("pipeline_ready", False)),
        artifact_current=bool(readiness.get("artifact_current", False)),
        data_certified_ready=bool(
            readiness.get(
                "data_certified_ready",
                readiness.get("certified_ready", False),
            )
        ),
        flow_certified_ready=flow_certified,
        execution_ready=bool(readiness.get("execution_ready", False)),
        run_status="reviewed",
        blockers=(readiness.get("missing_groups") or [])
        + (flow_health.get("blockers") or []),
        warnings=flow_health.get("warnings") or [],
    )
    readiness.update(operator_state)
    readiness["operator_state"] = dict(operator_state)

    suggested = regime[0].get("suggested_position_pct") if regime else None
    risk_position = risk[0].get("total_position_pct") if risk else None
    source_ready = bool(readiness.get("source_ready", readiness.get("analytics_ready", readiness.get("ready", False))))
    pipeline_ready = bool(readiness.get("pipeline_ready", source_ready))
    artifact_current = bool(readiness.get("artifact_current", False))
    data_certified_ready = bool(
        readiness.get("data_certified_ready", source_ready and pipeline_ready and artifact_current)
    )
    flow_certified_ready = readiness.get("flow_certified_ready")
    analysis_ready = bool(
        readiness.get(
            "analysis_ready",
            data_certified_ready
            and (flow_certified_ready is None or bool(flow_certified_ready)),
        )
    )
    # The explicit analysis gate is the operator decision.  The legacy aliases
    # below remain in the payload for older consumers only.
    certified_ready = analysis_ready
    analytics_ready = analysis_ready
    execution_ready = bool(readiness.get("execution_ready"))
    effective_position = suggested if analysis_ready else 0

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
        "concept_limit_up": concept_limit_up,
        "market_context": market_context,
        "data_sources": data_sources,
        "capital_flow_health": flow_health,
        "ecology": ecology,
        "sector_trail": sector_trail,
        "sector_periods": sector_periods,
        "readiness": readiness,
        "execution_control": {
            "source_ready": source_ready,
            "pipeline_ready": pipeline_ready,
            "artifact_current": artifact_current,
            "data_certified_ready": data_certified_ready,
            "flow_certified_ready": flow_certified_ready,
            "analysis_ready": analysis_ready,
            "operator_status": readiness.get("operator_status", "uncertified"),
            "certified_ready": certified_ready,
            "analytics_ready": analytics_ready,
            "execution_ready": execution_ready,
            "effective_position_pct": effective_position,
            "suggested_position_pct": suggested,
            "risk_position_pct": risk_position,
            "override": "ALLOW_REVIEW_ONLY" if analysis_ready and not execution_ready else "ALLOW" if execution_ready else "BLOCK",
        },
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
    stock_money = ("main_net", "super_net", "large_net")
    sector_money = ("main_net",)
    lines.extend(_table_rows(_format_flow_rows(flow.get("stock_inflow", []), stock_money), ["stock_code", "stock_name", "main_net", "super_net", "large_net", "change_pct", "provider"], "No stock inflow rows"))
    lines.extend(
        [
            "",
            "### Individual Stock Main-Net Outflow Top 50",
            "",
            "| Code | Name | Main Net | Super | Large | Change % | Provider |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("stock_outflow", []), stock_money), ["stock_code", "stock_name", "main_net", "super_net", "large_net", "change_pct", "provider"], "No stock outflow rows"))
    lines.extend(
        [
            "",
            f"### THS Concept Flow Inflow Top 10 ({sector_title} rows)",
            "",
            "| Concept | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("sector_inflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], f"No {sector_title.lower()} inflow rows"))
    lines.extend(
        [
            "",
            f"### THS Concept Flow Outflow Top 10 ({sector_title} rows)",
            "",
            "| Concept | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("sector_outflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], f"No {sector_title.lower()} outflow rows"))
    lines.extend(
        [
            "",
            "### Eastmoney Industry Flow Inflow Top 10",
            "",
            "| Industry | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("industry_inflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], "No industry inflow rows"))
    lines.extend(
        [
            "",
            "### Eastmoney Industry Flow Outflow Top 10",
            "",
            "| Industry | Main Net | Change % | Provider |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(_table_rows(_format_flow_rows(flow.get("industry_outflow", []), sector_money), ["sector_name", "main_net", "change_pct", "provider"], "No industry outflow rows"))
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
    lines.extend(
        [
            "",
            "### Capital Flow Persistence (20-Day Evidence)",
            "",
            "| Stock | Observed | Positive | 3D | 5D | 10D | 20D | Acceleration | Latest |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    persistence_money = ("main_net_3d", "main_net_5d", "main_net_10d", "twenty_day_main_net", "flow_acceleration_5d")
    persistence = []
    for row in flow.get("stock_flow_persistence", []):
        item = dict(row)
        for field in persistence_money:
            item[field] = _fmt_money(item.get(field))
        persistence.append(item)
    lines.extend(_table_rows(persistence, ["stock_code", "observed_days_20d", "positive_days", "main_net_3d", "main_net_5d", "main_net_10d", "twenty_day_main_net", "flow_acceleration_5d", "latest_date"], "No stock persistence rows"))
    lines.extend(
        [
            "",
            "### Capital Flow Persistence Outflow (20-Day Evidence)",
            "",
            "| Stock | Observed | Positive | 3D | 5D | 10D | 20D | Acceleration | Latest |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    persistence_outflow = []
    for row in flow.get("stock_flow_persistence_outflow", []):
        item = dict(row)
        for field in persistence_money:
            item[field] = _fmt_money(item.get(field))
        persistence_outflow.append(item)
    lines.extend(_table_rows(persistence_outflow, ["stock_code", "observed_days_20d", "positive_days", "main_net_3d", "main_net_5d", "main_net_10d", "twenty_day_main_net", "flow_acceleration_5d", "latest_date"], "No stock outflow persistence rows"))
    lines.extend(
        [
            "",
            "### THS Concept Persistence (20-Day Evidence)",
            "",
            "| Concept | Observed | Positive | 3D | 5D | 10D | 20D | Acceleration | Latest |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    sector_persistence = []
    for row in flow.get("sector_flow_persistence", []):
        item = dict(row)
        for field in persistence_money:
            item[field] = _fmt_money(item.get(field))
        sector_persistence.append(item)
    lines.extend(_table_rows(sector_persistence, ["sector_name", "observed_days_20d", "positive_days", "main_net_3d", "main_net_5d", "main_net_10d", "twenty_day_main_net", "flow_acceleration_5d", "latest_date"], "No concept persistence rows"))
    lines.extend(
        [
            "",
            "### THS Concept Persistence Outflow (20-Day Evidence)",
            "",
            "| Concept | Observed | Positive | 3D | 5D | 10D | 20D | Acceleration | Latest |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    sector_persistence_outflow = []
    for row in flow.get("sector_flow_persistence_outflow", []):
        item = dict(row)
        for field in persistence_money:
            item[field] = _fmt_money(item.get(field))
        sector_persistence_outflow.append(item)
    lines.extend(_table_rows(sector_persistence_outflow, ["sector_name", "observed_days_20d", "positive_days", "main_net_3d", "main_net_5d", "main_net_10d", "twenty_day_main_net", "flow_acceleration_5d", "latest_date"], "No concept outflow persistence rows"))
    lines.extend(
        [
            "",
            "### 龙虎榜 (Post-Market Evidence)",
            "",
            "| Stock | Change | Reason | Buy | Sell | Net |",
            "|---|---:|---|---:|---:|---:|",
        ]
    )
    lhb_rows = [
        {**row, "buy_amount": _fmt_money(row.get("buy_amount")),
         "sell_amount": _fmt_money(row.get("sell_amount")),
         "net_amount": _fmt_money(row.get("net_amount"))}
        for row in flow.get("lhb", [])
    ]
    lines.extend(_table_rows(lhb_rows, ["stock_name", "change_pct", "reason", "buy_amount", "sell_amount", "net_amount"], "No LHB rows for this date"))
    return lines


def render_daily_review_markdown(context: dict) -> str:
    regime = context.get("regime", {})
    risk = context.get("risk", {})
    stats = context.get("statistics", {})
    backfill = context.get("backfill", {})
    flow = context.get("capital_flow", {})
    readiness = context.get("readiness", {})
    control = context.get("execution_control", {})
    market_context = context.get("market_context", {})
    data_sources = context.get("data_sources", {})
    story = build_review_narrative(context)
    lines = [
        f"# Daily Review - {context.get('trade_date', '')}",
        "",
        "## 今日裁决",
        "",
        f"- 裁决：`{story.get('stance_label')}`",
        f"- 一句话：`{story.get('headline')}`",
        f"- 说明：{story.get('lede')}",
        f"- 主线：`{story.get('mainline')}`",
        f"- 有效仓位：`{story.get('effective_position_pct')}%`",
        f"- 缺失：`{', '.join(story.get('missing') or []) or 'none'}`",
    ]
    for bullet in story.get("bullets") or []:
        lines.append(f"- {bullet}")
    lines.extend(["", "### 次日只看这几件事", ""])
    for item in story.get("tomorrow") or []:
        lines.append(f"- {item}")
    ecology = context.get("ecology") or {}
    lines.extend(["", "### 连板梯队", "", "| 高度 | 家数 | 股票 | 备注 |", "|---:|---:|---|---|"])
    for group in ecology.get("ladder_groups") or []:
        names = "、".join(str(n) for n in (group.get("names") or [])[:8])
        if (group.get("count") or 0) > 8:
            names += f" 等{group.get('count')}只"
        lines.append(f"| {group.get('height')}板 | {group.get('count')} | {names} | {group.get('note') or ''} |")
    if not ecology.get("ladder_groups"):
        lines.append("|  |  | 暂无连板名单 |  |")
    trail = context.get("sector_trail") or {}
    trail_dates = (trail.get("dates") or [])[:6]
    lines.extend(["", "### 板块轨迹", "", f"- 成分快照：`{trail.get('membership_date') or '—'}`；对比日 `{', '.join(trail_dates) or 'none'}`。"])
    if trail_dates:
        header = "| 概念 | " + " | ".join(d[5:] if len(d) >= 10 else d for d in trail_dates) + " |"
        align = "|---|" + "|".join("---:" for _ in trail_dates) + "|"
        lines.extend(["", header, align])
        for sector in (trail.get("sectors") or [])[:12]:
            cells = [str(sector.get("name") or "")]
            daily = sector.get("daily") or {}
            for day in trail_dates:
                cell = daily.get(day) or {}
                lu = cell.get("limit_up")
                cells.append(str(lu) if lu is not None else "—")
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("- 暂无板块轨迹")
    periods = context.get("sector_periods") or {}
    for kind, title in (("week", "周聚合"), ("month", "月聚合")):
        block = periods.get(kind) or {}
        labels = [item.get("label") for item in (block.get("periods") or [])[:6]]
        lines.extend(["", f"### 板块轨迹 · {title}", ""])
        if labels:
            header = "| 概念 | " + " | ".join(str(label) for label in labels) + " |"
            align = "|---|" + "|".join("---:" for _ in labels) + "|"
            lines.extend([header, align])
            for sector in (block.get("sectors") or [])[:10]:
                cells = [str(sector.get("name") or "")]
                pdata = sector.get("periods") or {}
                for label in labels:
                    cell = pdata.get(label) or {}
                    lu = cell.get("limit_up")
                    cells.append(str(lu) if lu is not None else "—")
                lines.append("| " + " | ".join(cells) + " |")
        else:
            lines.append("- 暂无期间数据")
    yday = ecology.get("yday") or {}
    lines.extend(
        [
            "",
            "### 昨日涨停今日表现",
            "",
            f"- 样本：`{yday.get('n') or 0}` 只（对比日 `{yday.get('prev_date') or '—'}`）",
            f"- 平均涨幅：`{_fmt_pct(yday.get('avg_ret')) or '—'}`；正收益 `{_fmt_pct(yday.get('pos_rate')) or '—'}`",
            f"- 继续涨停：`{yday.get('still_limit_up') or 0}`；跌停 `{yday.get('limit_down') or 0}`",
            f"- 首板均涨：`{_fmt_pct(yday.get('first_avg')) or '—'}`；多板均涨 `{_fmt_pct(yday.get('multi_avg')) or '—'}`",
            "",
            "### 接力失败（昨 2 板+ 今日未封）",
            "",
            "| 股票 | 昨板 | 今日涨幅 |",
            "|---|---:|---:|",
        ]
    )
    broken_rows = [
        {
            "stock": row.get("stock_name") or row.get("stock_code"),
            "board_level": row.get("board_level"),
            "change_pct": _fmt_pct(row.get("change_pct")),
        }
        for row in (ecology.get("broken") or [])
    ]
    lines.extend(_table_rows(broken_rows, ["stock", "board_level", "change_pct"], "No failed-continuation rows"))
    lines.extend(
        [
            "",
            "## Market Regime",
        "",
        f"- Regime: `{regime.get('regime', 'unknown')}`",
        f"- Regime score: `{regime.get('regime_score', '')}`",
        f"- Suggested position: `{regime.get('suggested_position_pct', '')}%`",
        f"- Risk state: `{risk.get('risk_state', 'unknown')}`",
        "",
        "## P0 Data And Execution Gate",
        "",
        f"- Source ready: `{str(readiness.get('source_ready', readiness.get('analytics_ready', False))).lower()}`; pipeline ready: `{str(readiness.get('pipeline_ready', False)).lower()}`; artifact current: `{str(readiness.get('artifact_current', False)).lower()}`.",
        f"- Data certified: `{str(readiness.get('data_certified_ready', False)).lower()}`; flow certified: `{str(readiness.get('flow_certified_ready', 'not_assessed')).lower()}`; analysis ready: `{str(readiness.get('analysis_ready', readiness.get('certified_ready', False))).lower()}`; operator status: `{readiness.get('operator_status', 'uncertified')}`.",
        f"- Control: `{control.get('override', 'BLOCK')}`; effective position cap: `{control.get('effective_position_pct', 0)}%`.",
        f"- Actionable / tradable / risk-approved / executable: `{readiness.get('actionable_candidates', 0)} / {readiness.get('tradable_candidates', 0)} / {readiness.get('risk_approved_candidates', 0)} / {readiness.get('executable_candidates', 0)}`.",
        f"- Missing groups: `{', '.join(readiness.get('missing_groups', [])) or 'none'}`.",
        "- Review basis: same-date postmarket snapshot; use the live freshness gate before any executable decision.",
        ]
    )
    lines.extend(
        [
            "",
            "## Market Breadth And Auction Context",
            "",
            "| Evidence | Values |",
            "|---|---|",
        ]
    )
    breadth_rows = market_context.get("breadth", [])
    if breadth_rows:
        for row in breadth_rows:
            values = ", ".join(f"{key}={value}" for key, value in row.items() if value not in (None, ""))
            lines.append(f"| breadth | {values} |")
    else:
        lines.append("| breadth | no same-date breadth snapshot |")
    for row in market_context.get("limit_summary", []):
        values = ", ".join(f"{key}={value}" for key, value in row.items() if value not in (None, ""))
        lines.append(f"| limit-up/down | {values} |")
    for row in market_context.get("auction", []):
        values = ", ".join(f"{key}={value}" for key, value in row.items() if value not in (None, ""))
        lines.append(f"| auction anomaly | {values} |")
    lhb_summary = market_context.get("lhb_summary") or {}
    lines.append(f"| LHB | rows={lhb_summary.get('rows', 0)}, stocks={lhb_summary.get('stocks', 0)}, fetched={lhb_summary.get('fetched_at') or '-'} |")
    lines.extend(["", "## Provider And Research Checkpoints", "", "| Dataset | Status / Evidence |", "|---|---|"])
    tushare_rows = data_sources.get("tushare", [])
    if tushare_rows:
        for row in tushare_rows:
            detail = f"status={row.get('status')}, rows={row.get('rows_written', 0)}, attempts={row.get('attempts', 0)}, error={row.get('last_error') or '-'}"
            lines.append(f"| {row.get('dataset')} | {detail} |")
    else:
        lines.append("| TuShare checkpoints | no same-date checkpoint |")
    for row in data_sources.get("kline", []):
        lines.append(f"| {row.get('relation')} | latest={row.get('latest') or '-'}, same-date rows={row.get('same_date_rows', 0)} |")
    for relation, row in (data_sources.get("flow_features") or {}).items():
        lines.append(
            f"| {relation} | rows={row.get('rows', 0)}, dates={row.get('dates', 0)}, "
            f"latest={row.get('latest') or '-'}, version={row.get('version') or '-'} |"
        )
    ths = data_sources.get("ths") or {}
    if ths:
        lines.append(
            f"| THS membership | snapshot={ths.get('trade_date')}, raw={ths.get('concepts', 0)} concepts/{ths.get('members', 0)} members, "
            f"usable={ths.get('usable_concepts', 0)} concepts/{ths.get('usable_members', 0)} members, "
            f"checkpoint={ths.get('success', 0)}/{ths.get('checkpoint_rows', 0)}, partial/stale={ths.get('partial', 0)} |"
        )
    lines.append(f"| Operator outcomes | same-date rows={data_sources.get('outcomes', 0)}; zero means proxy/backtest only |")
    for row in data_sources.get("qlib", [])[:5]:
        lines.append(
            f"| QLib shadow {row.get('model_id') or '-'} | stage={row.get('stage')}, "
            f"samples={row.get('sample_count', 0)}, hit={row.get('hit_rate')}, "
            f"avg={row.get('avg_forward_return_pct')}, IC={row.get('ic')}, "
            f"RankIC={row.get('rank_ic')}, spread={row.get('top_bottom_spread')}, "
            f"drawdown={row.get('max_drawdown')}, impact={row.get('signal_impact')} |"
        )
    if not data_sources.get("qlib"):
        lines.append("| QLib shadow | no evaluation rows; optional dependency/model signal is not evidence for execution |")
    for row in data_sources.get("strategy", [])[:5]:
        lines.append(f"| Strategy {row.get('strategy_id') or '-'} | stage={row.get('stage')}, samples={row.get('sample_count', 0)}, win={row.get('win_rate')}, avg={row.get('avg_return_pct')}, verdict={row.get('verdict')} |")
    if not data_sources.get("strategy"):
        lines.append("| Strategy backtest | no stored summary rows |")
    lines.extend(
        [
            "",
            "## AI Review Contract",
            "",
            "- Facts snapshot: `reports/ai_review_facts_latest.json`",
            "- Status: `facts_ready_no_model_call`; deterministic facts are ready for an optional local/remote model.",
            "- Boundary: AI may summarize and explain sourced facts, but cannot fill missing data, change readiness, or turn QLib shadow scores into orders.",
        ]
    )
    lines.extend([""] + _render_flow_review_sections(flow))
    lines.extend(["", "## Mainline Themes", "", "| Sector | Score | Strength | Limit Up |", "|---|---:|---:|---:|"])
    lines.extend(_table_rows(context.get("sectors", []), ["sector_name", "score", "strength_value", "limit_up_count"]))

    concept_review = context.get("concept_limit_up") or {}
    lines.extend(
        [
            "",
            "## Concept Limit-Up Drilldown",
            "",
            f"- Membership snapshot: `{concept_review.get('membership_date') or 'unavailable'}`; limit pool date: `{concept_review.get('limit_date') or context.get('trade_date')}`.",
            "- The concept ranking is evidence for review only; same-date limit-up stocks are recomputed from the membership snapshot and limit pool.",
            "",
            "| Concept | Mainline | Limit Up | Max Board | Limit-Up Stocks |",
            "|---|---:|---:|---:|---|",
        ]
    )
    concept_rows = []
    for group in concept_review.get("groups", [])[:12]:
        stocks = ", ".join(
            f"{row.get('stock_code')} {row.get('stock_name') or ''}".strip()
            for row in group.get("limit_up_stocks", [])[:12]
        )
        concept_rows.append(
            {
                "concept": group.get("concept_name"),
                "mainline_score": group.get("mainline_score"),
                "limit_up_count": group.get("limit_up_count"),
                "max_board": group.get("max_board"),
                "limit_up_stocks": stocks,
            }
        )
    lines.extend(_table_rows(concept_rows, ["concept", "mainline_score", "limit_up_count", "max_board", "limit_up_stocks"], "No concept limit-up drilldown rows"))

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

    lines.extend(["", "## Next-Day Focus", ""])
    for item in story.get("tomorrow") or []:
        lines.append(f"- {item}")
    if not story.get("tomorrow"):
        lines.extend(
            [
                "- Recheck top mainline themes before auction.",
                "- Keep candidates only if auction and intraday evidence confirm the thesis.",
                "- Respect risk_snapshot max position before any manual action.",
            ]
        )
    lines.extend(
        [
            "",
            "## Data Gaps And Degradation",
            "",
        ]
    )
    for gap in flow.get("coverage_alerts", []):
        lines.append(f"- {gap}")
    if not readiness.get("pipeline_ready", readiness.get("analytics_ready", False)):
        lines.append("- P0: same-date evidence is incomplete; this report is review-only and no position should be opened from it.")
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
