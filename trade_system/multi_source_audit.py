"""Read-only readiness checks for the migrated multi-source tables."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import duckdb


TARGETS = {
    "ths_concept_daily": "THS_concepts_default",
    "ths_concept_stock_history": "THS_concept_members_default",
    "kpl_concept_daily": "KPL_concepts",
    "kpl_concept_stock_history": "KPL_concept_members",
    "multi_source_stock_flow": "个股资金流",
    "multi_source_sector_flow": "板块资金流",
    "multi_source_kline": "行情K线",
    "multi_source_quote": "实时估值/快照",
    "executable_quote_snapshot": "候选可执行报价快照",
    "multi_source_observation": "原始观测日志",
}


def audit_multisource(db_path: str | Path, as_of: str | None = None) -> dict:
    requested = as_of or date.today().isoformat()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {row[0] for row in con.execute("show tables").fetchall()}
        result = {"as_of": requested, "tables": {}, "capital_flow": {}, "checkpoints": [],
                  "concept_source": "ths"}
        as_of_date = datetime.strptime(requested, "%Y-%m-%d").date()
        for table, label in TARGETS.items():
            if table not in tables:
                result["tables"][table] = {"label": label, "status": "missing", "rows": 0}
                continue
            columns = {row[0] for row in con.execute(f"describe {table}").fetchall()}
            date_column = "source_date" if "source_date" in columns else ("trade_date" if "trade_date" in columns else None)
            rows = con.execute(f"select count(*) from {table}").fetchone()[0]
            latest = con.execute(f"select max({date_column}) from {table}").fetchone()[0] if date_column else None
            providers = []
            if "provider" in columns:
                providers = [row[0] for row in con.execute(f"select distinct provider from {table} where provider is not null order by 1").fetchall()]
            age_days = (as_of_date - latest).days if latest else None
            verified_rows = None
            if "date_verified" in columns:
                verified_rows = con.execute(f"select count(*) from {table} where date_verified").fetchone()[0]
            status = "empty" if not rows else ("unverified" if verified_rows == 0 else ("stale" if age_days is not None and age_days > 3 else "available"))
            result["tables"][table] = {
                "label": label, "status": status, "rows": rows, "verified_rows": verified_rows,
                "latest": str(latest) if latest else None, "age_days": age_days, "providers": providers,
            }

        if "ths_concept_member_checkpoint" in tables:
            # Checkpoints are append-only.  Current completeness must not use
            # all-history counts, so retain both views explicitly.
            cp = con.execute(
                "SELECT count(*), sum(CASE WHEN status='success' THEN 1 ELSE 0 END), "
                "sum(CASE WHEN status<>'success' THEN 1 ELSE 0 END), max(trade_date) "
                "FROM ths_concept_member_checkpoint"
            ).fetchone()
            latest_cp = con.execute(
                "SELECT max(trade_date) FROM ths_concept_member_checkpoint "
                "WHERE trade_date<=CAST(? AS DATE)", [requested]
            ).fetchone()[0]
            current_cp = con.execute(
                "SELECT count(*), sum(CASE WHEN status='success' THEN 1 ELSE 0 END), "
                "sum(CASE WHEN status<>'success' THEN 1 ELSE 0 END) "
                "FROM ths_concept_member_checkpoint WHERE trade_date=CAST(? AS DATE)",
                [latest_cp],
            ).fetchone() if latest_cp else (0, 0, 0)
            checkpoint = {"rows": int(current_cp[0] or 0), "success": int(current_cp[1] or 0),
                          "partial": int(current_cp[2] or 0), "latest": str(latest_cp) if latest_cp else None,
                          "history_rows": int(cp[0] or 0), "history_success": int(cp[1] or 0),
                          "history_partial": int(cp[2] or 0), "current_snapshot": True}
            result["concept_checkpoint"] = checkpoint
            if checkpoint["partial"] and "ths_concept_stock_history" in result["tables"]:
                result["tables"]["ths_concept_stock_history"]["status"] = "partial"
        else:
            result["concept_checkpoint"] = {"rows": 0, "success": 0, "partial": 0, "latest": None}

        flow = result["capital_flow"]
        for table, code_column in (("multi_source_stock_flow", "stock_code"), ("multi_source_sector_flow", "sector_code")):
            if table not in tables:
                flow[table] = {"status": "missing", "coverage": 0, "latest": None, "stale_rows": 0}
                continue
            coverage = con.execute(f"select count(distinct {code_column}) from {table}").fetchone()[0]
            latest = con.execute(f"select max(source_date) from {table}").fetchone()[0]
            latest_coverage = con.execute(
                f"select count(distinct {code_column}) from {table} where source_date=?", [latest]
            ).fetchone()[0] if latest else 0
            historical_peak = con.execute(
                f"select coalesce(max(day_coverage),0) from ("
                f"select source_date,count(distinct {code_column}) day_coverage from {table} group by source_date)"
            ).fetchone()[0]
            stale = con.execute(f"select count(*) from {table} where is_stale").fetchone()[0]
            age_days = (as_of_date - latest).days if latest else None
            partial = bool(latest and historical_peak and latest_coverage < max(1, historical_peak * 0.90))
            status = "empty" if not coverage else ("partial" if partial else ("stale" if age_days is not None and age_days > 3 else "available"))
            flow[table] = {"status": status, "coverage": coverage,
                           "latest": str(latest) if latest else None, "age_days": age_days, "stale_rows": stale,
                           "latest_coverage": latest_coverage, "historical_peak_coverage": historical_peak,
                           "coverage_pct_of_peak": round(latest_coverage * 100.0 / historical_peak, 2) if historical_peak else 0.0}
        if "multi_source_task_checkpoint" in tables:
            result["checkpoints"] = [
                {"stage": row[0], "status": row[1], "tasks": row[2], "last_updated": str(row[3]) if row[3] else None}
                for row in con.execute(
                    "SELECT stage,status,count(*),max(updated_at) FROM multi_source_task_checkpoint "
                    "GROUP BY stage,status ORDER BY stage,status"
                ).fetchall()
            ]
        # THS concept counts follow the same current snapshot as the
        # pagination checkpoint; cumulative rows are audit-only.
        if "ths_concept_daily" in tables:
            latest_concept = con.execute(
                "SELECT max(trade_date) FROM ths_concept_daily "
                "WHERE trade_date<=CAST(? AS DATE)", [requested]
            ).fetchone()[0]
            if latest_concept:
                raw_concept_count = con.execute(
                    "SELECT count(DISTINCT concept_code) FROM ths_concept_daily WHERE trade_date=?",
                    [latest_concept],
                ).fetchone()[0]
                raw_member_count = con.execute(
                    "SELECT count(*) FROM ths_concept_stock_history WHERE trade_date=?",
                    [latest_concept],
                ).fetchone()[0] if "ths_concept_stock_history" in tables else 0
                # The raw THS tables intentionally retain partial/stale
                # recovery rows for audit.  Default consumers must use the
                # quality-gated views, otherwise a stale cached board appears
                # complete in this report even though signals correctly omit
                # it.  Fall back to raw counts for legacy/test databases that
                # predate the canonical views.
                canonical_concepts = (
                    "v_default_concept_daily"
                    if "v_default_concept_daily" in tables else "ths_concept_daily"
                )
                canonical_members = (
                    "v_default_concept_stock_history"
                    if "v_default_concept_stock_history" in tables else "ths_concept_stock_history"
                )
                concept_count = con.execute(
                    f"SELECT count(DISTINCT concept_code) FROM {canonical_concepts} WHERE trade_date=?",
                    [latest_concept],
                ).fetchone()[0]
                member_count = con.execute(
                    f"SELECT count(*) FROM {canonical_members} WHERE trade_date=?",
                    [latest_concept],
                ).fetchone()[0] if canonical_members in tables else 0
                result["concept_snapshot"] = {
                    "trade_date": str(latest_concept),
                    "concepts": int(concept_count or 0),
                    "members": int(member_count or 0),
                    "raw_concepts": int(raw_concept_count or 0),
                    "raw_members": int(raw_member_count or 0),
                    "status": "partial" if (result.get("concept_checkpoint") or {}).get("partial") else "available",
                }
                result["tables"]["ths_concept_daily"].update(
                    current_rows=int(concept_count or 0),
                    current_status=result["concept_snapshot"]["status"],
                )
                if "ths_concept_stock_history" in result["tables"]:
                    result["tables"]["ths_concept_stock_history"].update(
                        current_rows=int(member_count or 0),
                        current_status=result["concept_snapshot"]["status"],
                    )
            else:
                result["concept_snapshot"] = {"trade_date": None, "concepts": 0, "members": 0, "status": "missing"}
        return result
    finally:
        con.close()


def render_multisource_readiness(result: dict) -> str:
    lines = ["# 多源数据及时性审计", "", f"- as_of: `{result['as_of']}`", "",
             "| 数据域 | 状态 | 行数 | 最新日期 | 距 as_of 天数 | provider |", "|---|---|---:|---|---:|---|"]
    for table, item in result["tables"].items():
        age = item.get('age_days') if item.get('age_days') is not None else '-'
        display_rows = item.get('current_rows', item.get('rows', 0))
        display_status = item.get('current_status', item['status'])
        lines.append(f"| {item['label']} | {display_status} | {display_rows} | {item.get('latest') or '-'} | {age} | {', '.join(item.get('providers', [])) or '-'} |")
    legacy_quote = result["tables"].get("multi_source_quote") or {}
    executable_quote = result["tables"].get("executable_quote_snapshot") or {}
    if legacy_quote.get("status") in {"stale", "empty", "missing"} and executable_quote.get("status") == "available":
        lines.extend([
            "",
            "- `multi_source_quote` 是历史补充表，当前可执行报价以 `executable_quote_snapshot` 为准；两者不应混作同一口径。",
        ])
    if any(item.get("status") == "unverified" for item in result["tables"].values()):
        lines.extend(["", "- 当前默认概念源为 THS：热榜接口不回显历史有效日期，`unverified` 记录只能作为抓取日快照参考，不可直接用于历史回测。",
                      "- KPL 表保留为兼容/备选源，不再作为默认概念源。"])
    snapshot = result.get("concept_snapshot") or {}
    if snapshot:
        lines.extend([
            "", "## THS current snapshot", "",
            f"- Current snapshot: `{snapshot.get('trade_date') or '-'}`; concepts=`{snapshot.get('concepts', 0)}`, members=`{snapshot.get('members', 0)}`, status=`{snapshot.get('status', 'missing')}`.",
            f"- Raw snapshot retained for audit: concepts=`{snapshot.get('raw_concepts', snapshot.get('concepts', 0))}`, members=`{snapshot.get('raw_members', snapshot.get('members', 0))}`; default views exclude partial/stale boards.",
            "- Historical cumulative counts are audit-only and are not used as current completeness evidence.",
        ])
    cp = result.get("concept_checkpoint") or {}
    if cp.get("partial"):
        lines.append(f"- THS 成分分页 checkpoint: `{cp.get('success', 0)}/{cp.get('rows', 0)} success`, partial={cp.get('partial', 0)}; anti-bot/empty pages are not complete.")
    lines.extend(["", "## 资金流重点", "", "| 表 | 状态 | 历史覆盖标的数 | 最新日覆盖 | 历史峰值覆盖 | 覆盖率 | 最新日期 | 距 as_of 天数 | stale 行数 |", "|---|---|---:|---:|---:|---:|---|---:|---:|"])
    for table, item in result["capital_flow"].items():
        age = item.get('age_days') if item.get('age_days') is not None else '-'
        lines.append(f"| {table} | {item['status']} | {item['coverage']} | {item.get('latest_coverage', 0)} | {item.get('historical_peak_coverage', 0)} | {item.get('coverage_pct_of_peak', 0)}% | {item.get('latest') or '-'} | {age} | {item['stale_rows']} |")
    lines.extend(["", "## 判定规则", "", "- `available` 只表示表内有数据，不等于当前交易日一定完整；必须同时看最新日期、覆盖数和 `stale_rows`。",
                  "- 资金流必须分别看个股表与板块表，不能用个股资金流推断板块资金流。", ""])
    if result.get("checkpoints"):
        lines.extend(["", "## Staged task checkpoints", "", "| stage | status | tasks | last updated |", "|---|---|---:|---|"])
        for item in result["checkpoints"]:
            lines.append(f"| {item['stage']} | {item['status']} | {item['tasks']} | {item.get('last_updated') or '-'} |")
    return "\n".join(lines)
