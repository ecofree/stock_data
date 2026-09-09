"""Markdown report generation for operator-facing trading review."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.backtest import run_market_regime_backtest
from trade_system.quality import run_quality_audit, table_columns, table_exists
from trade_system.signals import generate_signals
from trade_system.db_utils import fetch_dicts as _fetch_dicts


def _safe_json_text(value) -> str:
    if value is None:
        return "{}"
    return str(value)


def render_daily_report(
    trade_date: str,
    quality: dict,
    regime: dict,
    sectors: list[dict],
    candidates: list[dict],
    alerts: list[dict],
    backtest: dict,
) -> str:
    lines = [
        f"# 盘前/盘后交易辅助报告 - {trade_date}",
        "",
        "## 市场状态",
        "",
        f"- 情绪阶段: {regime.get('regime', '未知')}",
        f"- 建议仓位上限: {regime.get('suggested_position_pct', 0)}%",
        "",
        "## 数据质量",
        "",
        f"- 表数量: {quality.get('summary', {}).get('table_count', 0)}",
        f"- 总行数: {quality.get('summary', {}).get('total_rows', 0)}",
        f"- 重复问题表数: {quality.get('summary', {}).get('duplicate_issue_count', 0)}",
        "",
        "## 主线板块",
        "",
        "| Rank | Sector | Score |",
        "|---:|---|---:|",
    ]
    for idx, item in enumerate(sectors[:10], start=1):
        lines.append(f"| {idx} | {item.get('sector_name') or item.get('sector_code') or ''} | {item.get('score', 0):.1f} |")
    lines.extend(["", "## 候选股票", "", "| Rank | Stock | Score | Source |", "|---:|---|---:|---|"])
    for idx, item in enumerate(candidates[:20], start=1):
        lines.append(
            f"| {idx} | {item.get('stock_name') or item.get('stock_code') or ''} | "
            f"{item.get('score', 0):.1f} | {item.get('source', '')} |"
        )
    lines.extend(["", "## 风险告警", "", "| Severity | Message |", "|---|---|"])
    for item in alerts:
        lines.append(f"| {item.get('severity', '')} | {item.get('message', '')} |")
    lines.extend(
        [
            "",
            "## 回测概览",
            "",
            f"- 样本数: {backtest.get('sample_count', 0)}",
            f"- 阶段分布: {backtest.get('regime_counts', {})}",
            "",
        ]
    )
    return "\n".join(lines)


def render_market_status_report(trade_date: str, regime: dict) -> str:
    return "\n".join(
        [
            f"# 市场状态报告 - {trade_date}",
            "",
            f"- 情绪阶段: {regime.get('regime', '未知')}",
            f"- 建议仓位上限: {regime.get('suggested_position_pct', 0)}%",
            f"- 情绪分: {regime.get('regime_score', '')}",
            "",
            "## 证据",
            "",
            "```json",
            _safe_json_text(regime.get("evidence_json")),
            "```",
            "",
        ]
    )


def render_sector_mainline_report(trade_date: str, sectors: list[dict]) -> str:
    lines = [
        f"# 板块主线报告 - {trade_date}",
        "",
        "| Rank | Sector | Score | Strength | Limit Up | Seal Rate |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for idx, item in enumerate(sectors, start=1):
        lines.append(
            f"| {idx} | {item.get('sector_name') or item.get('sector_code') or ''} | "
            f"{float(item.get('score') or 0):.1f} | {float(item.get('strength_value') or 0):.1f} | "
            f"{int(item.get('limit_up_count') or 0)} | {float(item.get('seal_rate') or 0):.1f} |"
        )
    lines.extend(["", "## 分数来源", ""])
    for idx, item in enumerate(sectors[:10], start=1):
        lines.extend(
            [
                f"### {idx}. {item.get('sector_name') or item.get('sector_code') or ''}",
                "",
                "```json",
                _safe_json_text(item.get("evidence_json")),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def render_candidate_report(trade_date: str, candidates: list[dict]) -> str:
    lines = [
        f"# 候选股报告 - {trade_date}",
        "",
        "| Rank | Stock | Score | Source |",
        "|---:|---|---:|---|",
    ]
    for idx, item in enumerate(candidates, start=1):
        lines.append(
            f"| {idx} | {item.get('stock_name') or item.get('stock_code') or ''} | "
            f"{float(item.get('score') or 0):.1f} | {item.get('source', '')} |"
        )
    lines.extend(["", "## 入选原因 / 风险点 / 失效条件", ""])
    for idx, item in enumerate(candidates[:20], start=1):
        lines.extend(
            [
                f"### {idx}. {item.get('stock_name') or item.get('stock_code') or ''}",
                "",
                "```json",
                _safe_json_text(item.get("evidence_json")),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def render_risk_alert_report(trade_date: str, alerts: list[dict]) -> str:
    lines = [
        f"# 风险告警报告 - {trade_date}",
        "",
        "| Severity | Category | Message |",
        "|---|---|---|",
    ]
    for item in alerts:
        lines.append(f"| {item.get('severity', '')} | {item.get('category', '')} | {item.get('message', '')} |")
    lines.extend(["", "## 证据", ""])
    for idx, item in enumerate(alerts, start=1):
        lines.extend(
            [
                f"### {idx}. {item.get('severity', '')} {item.get('category', '')}",
                "",
                "```json",
                _safe_json_text(item.get("evidence_json")),
                "```",
                "",
            ]
        )
    return "\n".join(lines)



def load_daily_report_context(
    db_path: str | Path,
    trade_date: str | None = None,
    readiness_stage: str = "close",
) -> dict:
    signal_result = generate_signals(db_path, trade_date, readiness_stage=readiness_stage)
    selected_date = signal_result["trade_date"]
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        regime = _fetch_dicts(
            con,
            "SELECT * FROM market_regime_snapshot WHERE trade_date = ? ORDER BY generated_at DESC LIMIT 1",
            [selected_date],
        )
        sector_columns = set(table_columns(con, "sector_rotation_score"))
        sector_filter = (
            "AND (taxonomy IN ('ths_concept','ths_concept_derived') "
            "OR (coalesce(taxonomy,'unknown')='unknown' AND sector_code LIKE 'THS-%'))"
            if "taxonomy" in sector_columns else "AND sector_code LIKE 'THS-%'"
        )
        sectors = _fetch_dicts(
            con,
            "SELECT * FROM sector_rotation_score WHERE trade_date = ? "
            + sector_filter
            + " ORDER BY score DESC LIMIT 20",
            [selected_date],
        )
        candidates = _fetch_dicts(
            con,
            "SELECT * FROM stock_candidate_score WHERE trade_date = ? ORDER BY score DESC LIMIT 50",
            [selected_date],
        )
        alerts = _fetch_dicts(
            con,
            "SELECT * FROM alert_events WHERE trade_date = ? ORDER BY severity, generated_at",
            [selected_date],
        )
        if not table_exists(con, "market_regime_snapshot"):
            regime = []
    finally:
        con.close()
    return {
        "trade_date": selected_date,
        "quality": run_quality_audit(db_path),
        "regime": regime[0] if regime else signal_result,
        "sectors": sectors,
        "candidates": candidates,
        "alerts": alerts,
        "backtest": run_market_regime_backtest(db_path),
    }


def write_daily_report(
    db_path: str | Path,
    out_path: str | Path,
    trade_date: str | None = None,
    readiness_stage: str = "close",
) -> Path:
    context = load_daily_report_context(db_path, trade_date, readiness_stage)
    report = render_daily_report(**context)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    return path


def write_professional_reports(
    db_path: str | Path,
    out_dir: str | Path = "reports",
    trade_date: str | None = None,
    readiness_stage: str = "close",
) -> dict[str, Path]:
    context = load_daily_report_context(db_path, trade_date, readiness_stage)
    selected_date = context["trade_date"]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "market_status": out / "market_status_latest.md",
        "sector_mainline": out / "sector_mainline_latest.md",
        "candidate_stocks": out / "candidate_stocks_latest.md",
        "risk_alerts": out / "risk_alerts_latest.md",
    }
    paths["market_status"].write_text(
        render_market_status_report(selected_date, context["regime"]),
        encoding="utf-8",
    )
    paths["sector_mainline"].write_text(
        render_sector_mainline_report(selected_date, context["sectors"]),
        encoding="utf-8",
    )
    paths["candidate_stocks"].write_text(
        render_candidate_report(selected_date, context["candidates"]),
        encoding="utf-8",
    )
    paths["risk_alerts"].write_text(
        render_risk_alert_report(selected_date, context["alerts"]),
        encoding="utf-8",
    )
    return paths
