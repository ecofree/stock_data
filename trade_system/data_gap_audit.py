"""Coverage and data-gap audit for the 2026 historical data layer."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
from trade_system.trading_calendar import open_session_dates


STOCK_TABLES = {
    "tushare_daily": ("date", "ts_code"),
    "tushare_daily_basic": ("date", "ts_code"),
    "tushare_moneyflow": ("date", "ts_code"),
}
INDUSTRY_TABLE = ("tushare_moneyflow_industry", "trade_date", "ts_code")


def _iso(value: str | date) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) < 8:
        raise ValueError(f"invalid date: {value}")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _weekday_dates(start: str, end: str) -> list[str]:
    current = datetime.strptime(start, "%Y-%m-%d").date()
    final = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    while current <= final:
        if current.weekday() < 5:
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _industry_flow_quality(con, start: str, end: str) -> dict[str, int]:
    """Separate raw zero-close theme boards from usable normalized flow rows."""
    if "tushare_moneyflow_industry" not in _table_names(con):
        return {"raw_rows": 0, "raw_invalid_rows": 0, "normalized_invalid_rows": 0}
    raw_invalid = int(con.execute(
        "SELECT count(*) FROM tushare_moneyflow_industry "
        "WHERE trade_date BETWEEN ? AND ? AND close IS NOT NULL AND close<=0",
        [start, end],
    ).fetchone()[0])
    normalized_invalid = 0
    if "multi_source_sector_flow" in _table_names(con):
        normalized_invalid = int(con.execute(
            "SELECT count(*) FROM multi_source_sector_flow m "
            "JOIN tushare_moneyflow_industry r ON m.source_date=r.trade_date AND m.sector_code=r.ts_code "
            "WHERE m.provider='tushare' AND m.source_date BETWEEN ? AND ? "
            "AND r.close IS NOT NULL AND r.close<=0",
            [start, end],
        ).fetchone()[0])
    raw_rows = int(con.execute(
        "SELECT count(*) FROM tushare_moneyflow_industry WHERE trade_date BETWEEN ? AND ?",
        [start, end],
    ).fetchone()[0])
    return {"raw_rows": raw_rows, "raw_invalid_rows": raw_invalid,
            "normalized_invalid_rows": normalized_invalid}


def _table_names(con) -> set[str]:
    return {row[0] for row in con.execute("show tables").fetchall()}


def _calendar_dates(con, start: str, end: str) -> list[str]:
    # Missing calendar data is an operational gap, not permission to invent
    # sessions from weekdays.
    return open_session_dates(con, start, end)


def _daily_coverage(con, table: str, date_col: str, code_col: str, expected_dates: list[str],
                    expected_codes_by_date: dict[str, int] | None = None) -> dict[str, Any]:
    if table not in _table_names(con):
        return {"table": table, "status": "missing_table", "rows": 0, "missing_dates": expected_dates,
                "low_coverage_dates": [], "min_rows": 0, "max_rows": 0, "distinct_codes": 0}
    rows = con.execute(
        f"SELECT CAST({date_col} AS VARCHAR), count(*), count(DISTINCT {code_col}) "
        f"FROM {table} WHERE {date_col} BETWEEN ? AND ? GROUP BY {date_col} ORDER BY {date_col}",
        [expected_dates[0], expected_dates[-1]],
    ).fetchall() if expected_dates else []
    by_date = {str(row[0])[:10]: (int(row[1]), int(row[2])) for row in rows}
    counts = [value[1] for value in by_date.values()]
    peak = max(counts or [0])
    # The relay commonly returns a successful-looking 5,000-row page.  An
    # exact 5,000 count is therefore suspicious whenever another date proves
    # that the universe is larger; it must not be treated as a complete day.
    suspected_cap_dates = [
        item for item in expected_dates
        if item in by_date and by_date[item][1] == 5000 and peak > 5000
    ]
    # A 95% threshold catches true partial fetches while allowing listed/unlisted
    # status changes and source-specific extra historical securities.
    threshold = max(1, int(peak * 0.95)) if peak else 1
    missing = [item for item in expected_dates if item not in by_date]
    low = []
    for item in expected_dates:
        if item not in by_date:
            continue
        expected_codes = (expected_codes_by_date or {}).get(item)
        required = max(1, int(expected_codes * 0.95)) if expected_codes else threshold
        if by_date[item][1] < required:
            low.append(item)
    duplicate_groups = con.execute(
        f"SELECT count(*) FROM (SELECT {date_col},{code_col},count(*) c FROM {table} "
        f"WHERE {date_col} BETWEEN ? AND ? GROUP BY {date_col},{code_col} HAVING c>1)",
        [expected_dates[0], expected_dates[-1]],
    ).fetchone()[0] if expected_dates else 0
    return {
        "table": table, "status": "ok", "rows": int(con.execute(
            f"SELECT count(*) FROM {table} WHERE {date_col} BETWEEN ? AND ?", [expected_dates[0], expected_dates[-1]]
        ).fetchone()[0]) if expected_dates else 0,
        "expected_dates": len(expected_dates), "actual_dates": len(by_date),
        "missing_dates": missing, "low_coverage_dates": low,
        "min_rows": min(counts or [0]), "max_rows": max(counts or [0]),
        "min_distinct_codes": min(counts or [0]), "max_distinct_codes": max(counts or [0]),
        "duplicate_groups": int(duplicate_groups), "daily_counts": by_date,
        "suspected_cap_dates": suspected_cap_dates,
    }


def _invalid_counts(con, start: str, end: str) -> dict[str, int]:
    checks = {
        "tushare_daily": "close IS NOT NULL AND (close<=0 OR high<low OR volume<0 OR turnover<0)",
        "tushare_daily_basic": "total_mv IS NOT NULL AND total_mv<0",
        "tushare_moneyflow_industry": "close IS NOT NULL AND close<=0",
    }
    out = {}
    for table, predicate in checks.items():
        if table not in _table_names(con):
            continue
        date_col = "trade_date" if table == "tushare_moneyflow_industry" else "date"
        out[table] = int(con.execute(
            f"SELECT count(*) FROM {table} WHERE {date_col} BETWEEN ? AND ? AND ({predicate})", [start, end]
        ).fetchone()[0])
    return out


def _flow_coverage(con, table: str, code_col: str, as_of: str) -> dict[str, Any]:
    if table not in _table_names(con):
        return {"table": table, "status": "missing_table", "latest": None, "latest_coverage": 0}
    latest = con.execute(f"SELECT max(source_date) FROM {table}").fetchone()[0]
    peak = con.execute(
        f"SELECT coalesce(max(n),0) FROM (SELECT source_date,count(DISTINCT {code_col}) n FROM {table} GROUP BY source_date)"
    ).fetchone()[0]
    latest_coverage = con.execute(
        f"SELECT count(DISTINCT {code_col}) FROM {table} WHERE source_date=?", [latest]
    ).fetchone()[0] if latest else 0
    age = (date.fromisoformat(as_of) - latest).days if latest else None
    ratio = round(latest_coverage * 100.0 / peak, 2) if peak else 0.0
    return {"table": table, "status": "partial" if peak and ratio < 90 else "available",
            "latest": str(latest) if latest else None, "latest_coverage": int(latest_coverage),
            "historical_peak_coverage": int(peak), "coverage_pct_of_peak": ratio, "age_days": age}


def build_data_gap_audit(db_path: str | Path, start_date: str = "20260101", end_date: str = "20260714") -> dict[str, Any]:
    start, end = _iso(start_date), _iso(end_date)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        expected = _calendar_dates(con, start, end)
        latest_complete = expected[-1] if expected else None
        if latest_complete:
            expected = [item for item in expected if item <= latest_complete]
        # Do not use stock_basic as the expected universe here: that endpoint
        # can itself be capped at 5,000 rows and would make a truncated daily
        # response look complete.  Compare each dataset with its own observed
        # peak and separately report the stock-basic cap below.
        coverage = [_daily_coverage(con, table, date_col, code_col, expected)
                    for table, (date_col, code_col) in STOCK_TABLES.items()]
        industry = _daily_coverage(con, INDUSTRY_TABLE[0], INDUSTRY_TABLE[1], INDUSTRY_TABLE[2], expected)
        coverage.append(industry)
        invalid = _invalid_counts(con, expected[0], expected[-1]) if expected else {}
        industry_quality = _industry_flow_quality(con, expected[0], expected[-1]) if expected else {
            "raw_rows": 0, "raw_invalid_rows": 0, "normalized_invalid_rows": 0,
        }
        moneyflow_size_missing = 0
        if "tushare_moneyflow" in _table_names(con) and expected:
            moneyflow_size_missing = int(con.execute(
                "SELECT count(*) FROM tushare_moneyflow WHERE date BETWEEN ? AND ? AND "
                "(buy_sm_amount IS NULL OR sell_sm_amount IS NULL OR buy_md_amount IS NULL OR sell_md_amount IS NULL)",
                [expected[0], expected[-1]],
            ).fetchone()[0])
        stock_basic_count = int(con.execute("SELECT count(DISTINCT ts_code) FROM tushare_stock_basic").fetchone()[0]) if "tushare_stock_basic" in _table_names(con) else 0
        adj_codes = int(con.execute("SELECT count(DISTINCT ts_code) FROM tushare_adj_factor WHERE date BETWEEN ? AND ?", [start, end]).fetchone()[0]) if "tushare_adj_factor" in _table_names(con) else 0
        concept = {}
        for table in ("ths_concept_daily", "ths_concept_stock_history"):
            if table in _table_names(con):
                concept[table] = {
                    "rows": int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]),
                    "dates": int(con.execute(f"SELECT count(DISTINCT trade_date) FROM {table}").fetchone()[0]),
                    "verified_rows": int(con.execute(f"SELECT count(*) FROM {table} WHERE date_verified").fetchone()[0]),
                    "latest": str(con.execute(f"SELECT max(trade_date) FROM {table}").fetchone()[0]),
                }
            else:
                concept[table] = {"rows": 0, "dates": 0, "verified_rows": 0, "latest": None}
        if "ths_concept_member_checkpoint" in _table_names(con):
            cp = con.execute(
                "SELECT count(*), sum(CASE WHEN status='success' THEN 1 ELSE 0 END), "
                "sum(CASE WHEN status<>'success' THEN 1 ELSE 0 END), max(trade_date) "
                "FROM ths_concept_member_checkpoint"
            ).fetchone()
            concept["ths_member_checkpoint"] = {
                "rows": int(cp[0] or 0), "success": int(cp[1] or 0),
                "partial": int(cp[2] or 0), "latest": str(cp[3]) if cp[3] else None,
            }
        else:
            concept["ths_member_checkpoint"] = {"rows": 0, "success": 0, "partial": 0, "latest": None}
        flows = [_flow_coverage(con, "multi_source_stock_flow", "stock_code", end),
                 _flow_coverage(con, "multi_source_sector_flow", "sector_code", end)]
    finally:
        con.close()

    issues: list[str] = []
    for item in coverage:
        if item["status"] == "missing_table":
            issues.append(f"{item['table']} 表不存在")
        if item.get("missing_dates"):
            issues.append(f"{item['table']} 缺少 {len(item['missing_dates'])} 个交易日")
        if item.get("low_coverage_dates"):
            issues.append(f"{item['table']} 有 {len(item['low_coverage_dates'])} 个交易日覆盖不足")
        if item.get("suspected_cap_dates"):
            issues.append(
                f"{item['table']} 有 {len(item['suspected_cap_dates'])} 个交易日恰好返回5000条，疑似接口分页截断"
            )
        if item.get("duplicate_groups"):
            issues.append(f"{item['table']} 有 {item['duplicate_groups']} 组业务键重复")
    for table, count in invalid.items():
        if count:
            if table == "tushare_moneyflow_industry":
                issues.append(f"{table} 有 {count} 行 close=0 的主题型板块；资金字段为 0，不能当作可交易价格指数")
            else:
                issues.append(f"{table} 有 {count} 行价格/数值异常")
    if moneyflow_size_missing:
        issues.append(f"tushare_moneyflow 有 {moneyflow_size_missing} 行缺少小单/中单字段；主力净流仍可用，但订单结构分析不完整")
    if stock_basic_count and adj_codes < stock_basic_count * 0.8:
        issues.append(f"tushare_adj_factor 仅覆盖当前 stock_basic 的 {adj_codes}/{stock_basic_count} 只股票")
    if stock_basic_count == 5000:
        issues.append("tushare_stock_basic 恰好5000只，疑似接口分页上限；不能作为完整上市股票全集")
    if concept["ths_concept_daily"]["verified_rows"] == 0:
        issues.append("THS 概念快照没有任何 date_verified 行，2026 历史概念成分缺失")
    if concept["ths_member_checkpoint"].get("partial"):
        cp = concept["ths_member_checkpoint"]
        issues.append(f"THS member pagination checkpoint only {cp['success']}/{cp['rows']} success; {cp['partial']} concepts remain partial")
    for flow in flows:
        if flow["status"] == "partial":
            issues.append(f"{flow['table']} 最新日 {flow['latest']} 覆盖不完整（{flow['latest_coverage']}/{flow['historical_peak_coverage']}）")
    return {
        "start_date": start, "end_date": end, "latest_complete_date": latest_complete,
        "expected_trading_days": len(expected), "coverage": coverage, "invalid_values": invalid,
        "industry_flow_quality": industry_quality,
        "moneyflow_size_missing": moneyflow_size_missing,
        "stock_basic_codes": stock_basic_count, "adj_factor_codes": adj_codes,
        "concept": concept, "capital_flow": flows, "issues": issues,
    }


def render_data_gap_markdown(audit: dict[str, Any]) -> str:
    lines = ["# 2026 数据覆盖与缺失审计", "",
             f"- audit_range: `{audit['start_date']}` ~ `{audit['end_date']}`",
             f"- latest_complete_trading_date: `{audit.get('latest_complete_date') or '-'}`",
             f"- expected_trading_days: `{audit['expected_trading_days']}`",
             f"- issue_count: `{len(audit['issues'])}`", "", "## 日数据覆盖", "",
             "| table | dates actual/expected | rows min-max | missing dates | low coverage dates | suspected 5000-cap dates | duplicate groups |", "|---|---:|---:|---:|---:|---:|---:|"]
    for item in audit["coverage"]:
        lines.append(f"| {item['table']} | {item.get('actual_dates', 0)}/{item.get('expected_dates', 0)} | {item.get('min_rows', 0)}-{item.get('max_rows', 0)} | {len(item.get('missing_dates', []))} | {len(item.get('low_coverage_dates', []))} | {len(item.get('suspected_cap_dates', []))} | {item.get('duplicate_groups', 0)} |")
    lines.extend(["", "## 资金流最新覆盖", "", "| table | status | latest | latest coverage | historical peak | coverage % | age days |", "|---|---|---|---:|---:|---:|---:|"])
    for item in audit["capital_flow"]:
        lines.append(f"| {item['table']} | {item['status']} | {item.get('latest') or '-'} | {item.get('latest_coverage', 0)} | {item.get('historical_peak_coverage', 0)} | {item.get('coverage_pct_of_peak', 0)} | {item.get('age_days') if item.get('age_days') is not None else '-'} |")
    lines.extend(["", "## 关键缺失与问题", ""])
    quality = audit.get("industry_flow_quality") or {}
    lines.extend([
        "",
        "## Industry flow quality",
        "",
        f"- raw industry-flow rows: `{quality.get('raw_rows', 0)}`",
        f"- raw close<=0 theme/ranking rows retained for provenance: `{quality.get('raw_invalid_rows', 0)}`",
        f"- invalid rows still present in normalized sector flow: `{quality.get('normalized_invalid_rows', 0)}`",
        "",
        "## Data quality issues",
        "",
    ])
    lines.extend(f"- {issue}" for issue in audit["issues"])
    lines.extend(["", "## 概念源", "", f"- THS snapshot: `{audit['concept']['ths_concept_daily']}`", "- THS 热榜当前没有历史日期参数，概念历史日期缺失属于接口能力缺口，不得回填同一快照。", "- KPL 表仍可作为兼容源，但不是默认概念源。", ""])
    lines.append(f"- THS member checkpoint: `{audit['concept']['ths_member_checkpoint']}`")
    return "\n".join(lines)


def write_data_gap_report(db_path: str | Path, out_path: str | Path, start_date: str = "20260101", end_date: str = "20260714") -> dict[str, Any]:
    audit = build_data_gap_audit(db_path, start_date, end_date)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_data_gap_markdown(audit), encoding="utf-8")
    return audit
