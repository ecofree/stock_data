"""Retirement readiness checks for external projects after integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


RETIREMENT_COLUMNS = [
    "project",
    "capability",
    "migrated_to",
    "verified",
    "remaining_dependency",
    "delete_safe",
    "notes",
]


def _exists(root: Path, relative_path: str) -> bool:
    return (root / relative_path).exists()


def build_retirement_checklist(
    stock_root: str | Path,
    legacy_root: str | Path,
    tickflow_root: str | Path,
    vibe_root: str | Path,
) -> list[dict[str, Any]]:
    stock = Path(stock_root)
    legacy = Path(legacy_root)
    tickflow = Path(tickflow_root)
    vibe = Path(vibe_root)
    phase11_ready = _exists(stock, "reports/external_project_audit_latest.md") and _exists(
        stock, "docs/integration/external_capability_matrix.md"
    )
    return [
        {
            "project": "kpl_qds",
            "capability": "legacy_data_and_rules",
            "migrated_to": "legacy_qds_* tables; trade_system.integration; operator views",
            "verified": phase11_ready and _exists(stock, "reports/legacy_import_latest.md"),
            "remaining_dependency": "确认 stock_data 不再需要直接读取 kpl-qds 路径；删除前需备份 legacy DB。",
            "delete_safe": False,
            "notes": f"source_exists={legacy.exists()}; QMT 自动交易路径禁止迁入。",
        },
        {
            "project": "tickflow",
            "capability": "strategy_backtest_monitor_reference",
            "migrated_to": "trade_system.strategy; strategy_scan_result; strategy_backtest_result",
            "verified": _exists(stock, "reports/strategy_scan_latest.md")
            and _exists(stock, "reports/strategy_backtest_latest.md"),
            "remaining_dependency": "确认策略协议、回测约束和监控思想已满足；React 工作台仅参考。",
            "delete_safe": False,
            "notes": f"source_exists={tickflow.exists()}; 不迁移整套 FastAPI/React。",
        },
        {
            "project": "vibe",
            "capability": "research_news_reports_records",
            "migrated_to": "trade_system.research; news_radar_item; research_note; research_report_file",
            "verified": _exists(stock, "reports/news_radar_latest.md")
            and _exists(stock, "docs/integration/vibe_research_adapter.md"),
            "remaining_dependency": "确认新闻源、研报/公告和研究记录满足复盘需求；AI chat 不作为交易信号。",
            "delete_safe": False,
            "notes": f"source_exists={vibe.exists()}; global-stock-data 只作为外围参考。",
        },
    ]


def persist_retirement_checklist(db_path: str | Path, rows: list[dict[str, Any]]) -> int:
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS external_project_retirement_check (
                project VARCHAR,
                capability VARCHAR,
                migrated_to VARCHAR,
                verified BOOLEAN,
                remaining_dependency VARCHAR,
                delete_safe BOOLEAN,
                notes VARCHAR
            )
            """
        )
        con.execute("DELETE FROM external_project_retirement_check")
        for row in rows:
            values = [row.get(column) for column in RETIREMENT_COLUMNS]
            placeholders = ", ".join(["?"] * len(RETIREMENT_COLUMNS))
            column_sql = ", ".join(f'"{column}"' for column in RETIREMENT_COLUMNS)
            con.execute(
                f"INSERT INTO external_project_retirement_check ({column_sql}) VALUES ({placeholders})",
                values,
            )
        return len(rows)
    finally:
        con.close()


def render_retirement_checklist_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# 旧项目淘汰检查清单",
        "",
        "本清单只判断删除准备度，不执行删除。`delete_safe=false` 表示仍需人工确认或归档备份。",
        "",
        "| Project | Capability | Migrated to | Verified | delete_safe | Remaining dependency | Notes |",
        "|---|---|---|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['project']} | {row['capability']} | {row['migrated_to']} | "
            f"`{row['verified']}` | `{row['delete_safe']}` | {row['remaining_dependency']} | {row['notes']} |"
        )
    lines.extend(
        [
            "",
            "## 删除前必须满足",
            "",
            "- `stock_data` 一键日跑不依赖旧项目路径。",
            "- 所有迁移能力都有测试和报告。",
            "- 没有自动下单入口。",
            "- 没有硬编码 key 或敏感配置进入主项目。",
            "- 用户手动确认备份和删除范围。",
            "",
        ]
    )
    return "\n".join(lines)
