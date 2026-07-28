"""Read-only audit helpers for Phase 11 multi-project integration planning."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb


CAPABILITY_RULES: list[dict[str, str]] = [
    {
        "capability_id": "kpl_qds.legacy_data",
        "project": "kpl_qds",
        "module_path": "db/kpl_qds.duckdb",
        "capability_type": "data",
        "decision": "port",
        "target_adapter": "trade_system.integration.legacy_a_share",
        "operator_value": "补充 watchlist、涨停、板块资金、板块强度、龙虎榜和情绪样本。",
        "blocked_reason": "",
    },
    {
        "capability_id": "kpl_qds.signal_fusion",
        "project": "kpl_qds",
        "module_path": "engine/signal_fusion.py",
        "capability_type": "signal",
        "decision": "port",
        "target_adapter": "trade_system.signals evidence scoring",
        "operator_value": "迁移多信号融合思想，服务候选股入选原因、风险点和失效条件。",
        "blocked_reason": "",
    },
    {
        "capability_id": "kpl_qds.risk_enforcer",
        "project": "kpl_qds",
        "module_path": "engine/risk_enforcer.py",
        "capability_type": "risk",
        "decision": "port",
        "target_adapter": "trade_system.operator_risk",
        "operator_value": "迁移市场状态到仓位约束的规则思想。",
        "blocked_reason": "",
    },
    {
        "capability_id": "kpl_qds.auction_analyzer",
        "project": "kpl_qds",
        "module_path": "engine/auction_analyzer.py",
        "capability_type": "auction",
        "decision": "reference",
        "target_adapter": "trade_system.auction_deep",
        "operator_value": "参考竞价字段和分析框架；真实竞价 tick 仍需新数据源。",
        "blocked_reason": "",
    },
    {
        "capability_id": "kpl_qds.performance_tracker",
        "project": "kpl_qds",
        "module_path": "engine/performance_tracker.py",
        "capability_type": "review",
        "decision": "port",
        "target_adapter": "trade_system.review attribution",
        "operator_value": "迁移持仓、交易日志、已清仓归因思想。",
        "blocked_reason": "",
    },
    {
        "capability_id": "kpl_qds.qmt_bridge",
        "project": "kpl_qds",
        "module_path": "engine/qmt_bridge.py",
        "capability_type": "execution",
        "decision": "forbid",
        "target_adapter": "",
        "operator_value": "不服务本项目目标。",
        "blocked_reason": "自动交易/下单路径禁止进入 stock_data。",
    },
    {
        "capability_id": "kpl_qds.hardcoded_settings",
        "project": "kpl_qds",
        "module_path": "config/settings.yaml",
        "capability_type": "secret",
        "decision": "forbid",
        "target_adapter": "",
        "operator_value": "不服务操盘系统，且存在敏感配置风险。",
        "blocked_reason": "硬编码 key 或敏感配置禁止迁入，审计只记录文件存在，不读取内容。",
    },
    {
        "capability_id": "kpl_qds.qlib_shadow",
        "project": "kpl_qds",
        "module_path": "qlib_ext/inference_pipeline.py",
        "capability_type": "ml_shadow",
        "decision": "reference",
        "target_adapter": "trade_system.ml.qlib_shadow",
        "operator_value": "只允许作为旁路预测验证，不能直接参与交易信号。",
        "blocked_reason": "当前不迁移重型 qlib 训练/二进制数据。",
    },
    {
        "capability_id": "tickflow.strategy_engine",
        "project": "tickflow",
        "module_path": "backend/app/strategy/engine.py",
        "capability_type": "strategy",
        "decision": "port",
        "target_adapter": "trade_system.strategy",
        "operator_value": "迁移策略定义协议，服务盘前池、竞价确认、盘中强弱、尾盘去留。",
        "blocked_reason": "",
    },
    {
        "capability_id": "tickflow.indicator_pipeline",
        "project": "tickflow",
        "module_path": "backend/app/indicators/pipeline.py",
        "capability_type": "indicator",
        "decision": "port",
        "target_adapter": "trade_system.strategy.indicator_pipeline",
        "operator_value": "迁移 Polars/DuckDB 向量化指标思想。",
        "blocked_reason": "",
    },
    {
        "capability_id": "tickflow.backtest_engine",
        "project": "tickflow",
        "module_path": "backend/app/backtest/engine.py",
        "capability_type": "backtest",
        "decision": "port",
        "target_adapter": "trade_system.backtest.stage_backtest",
        "operator_value": "迁移 T+1、费用、滑点、仓位和组合约束思想。",
        "blocked_reason": "",
    },
    {
        "capability_id": "tickflow.monitor_rules",
        "project": "tickflow",
        "module_path": "backend/app/strategy/monitor.py",
        "capability_type": "monitor",
        "decision": "reference",
        "target_adapter": "trade_system.operator_risk alert rules",
        "operator_value": "参考监控规则、冷却和告警日志结构。",
        "blocked_reason": "",
    },
    {
        "capability_id": "tickflow.custom_data_source",
        "project": "tickflow",
        "module_path": "backend/app/data_providers/custom/provider.py",
        "capability_type": "adapter",
        "decision": "port",
        "target_adapter": "trade_system.integration.data_catalog",
        "operator_value": "迁移自定义数据源字段映射和降级策略。",
        "blocked_reason": "",
    },
    {
        "capability_id": "tickflow.react_workbench",
        "project": "tickflow",
        "module_path": "frontend/src/pages/Dashboard.tsx",
        "capability_type": "ui",
        "decision": "reference",
        "target_adapter": "reports/trading_dashboard_latest.html",
        "operator_value": "参考页面组织，不在当前阶段迁入 React 工作台。",
        "blocked_reason": "",
    },
    {
        "capability_id": "vibe.news_radar",
        "project": "vibe",
        "module_path": "backend/newsradar.py",
        "capability_type": "research",
        "decision": "port",
        "target_adapter": "trade_system.research.news_radar",
        "operator_value": "迁移资讯雷达，补题材催化和风险事件。",
        "blocked_reason": "",
    },
    {
        "capability_id": "vibe.a_stock_data",
        "project": "vibe",
        "module_path": "a-stock-data/SKILL.md",
        "capability_type": "data",
        "decision": "port",
        "target_adapter": "trade_system.adapters.vibe_research",
        "operator_value": "补公告、研报、热点原因、龙虎榜、资金和互动问答。",
        "blocked_reason": "",
    },
    {
        "capability_id": "vibe.research_records",
        "project": "vibe",
        "module_path": "backend/myreports.py",
        "capability_type": "review",
        "decision": "port",
        "target_adapter": "trade_system.research.report_registry",
        "operator_value": "迁移研报/附件索引和研究记录思想。",
        "blocked_reason": "",
    },
    {
        "capability_id": "vibe.portfolio_review",
        "project": "vibe",
        "module_path": "backend/portfolio.py",
        "capability_type": "review",
        "decision": "reference",
        "target_adapter": "trade_system.review positions",
        "operator_value": "参考持仓和已清仓归因结构。",
        "blocked_reason": "",
    },
    {
        "capability_id": "vibe.mcp_tools",
        "project": "vibe",
        "module_path": "backend/mcp_server.py",
        "capability_type": "tooling",
        "decision": "reference",
        "target_adapter": "future local tools",
        "operator_value": "参考本地数据工具暴露方式，不作为交易信号。",
        "blocked_reason": "",
    },
    {
        "capability_id": "vibe.fastapi_app",
        "project": "vibe",
        "module_path": "backend/app.py",
        "capability_type": "service",
        "decision": "reference",
        "target_adapter": "none",
        "operator_value": "参考接口组织，不迁移整套服务。",
        "blocked_reason": "",
    },
]


STOCK_CORE_RELATIONS = [
    "auction_tick",
    "sector_capital",
    "kline",
    "index_kline",
    "stock_candidate_stage_signal",
    "risk_snapshot",
]


def _path_status(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / Path(relative_path)
    return {
        "path": relative_path,
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() and path.is_file() else 0,
    }


def _count_rows(con: duckdb.DuckDBPyConnection, relation_name: str) -> int:
    try:
        return int(con.execute(f'SELECT count(*) FROM "{relation_name}"').fetchone()[0])
    except Exception:
        return 0


def _audit_duckdb_database(path: Path, preferred_relations: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "tables": {},
        "views": {},
    }
    if not path.exists():
        return result

    con = duckdb.connect(str(path), read_only=True)
    try:
        table_names = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
            ).fetchall()
        ]
        view_names = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.views WHERE table_schema='main' ORDER BY table_name"
            ).fetchall()
        ]
        selected_tables = table_names
        if preferred_relations is not None:
            selected_tables = [name for name in preferred_relations if name in table_names]
            selected_tables.extend(name for name in table_names if name not in selected_tables and name.startswith("legacy_qds_"))
        for name in selected_tables:
            result["tables"][name] = {"row_count": _count_rows(con, name)}
        for name in view_names:
            result["views"][name] = {"row_count": _count_rows(con, name)}
    finally:
        con.close()
    return result


def _audit_project_files(root: Path, relative_paths: list[str]) -> dict[str, dict[str, Any]]:
    return {relative_path: _path_status(root, relative_path) for relative_path in relative_paths}


def _audit_stock_data(root: Path) -> dict[str, Any]:
    reports_dir = root / "reports"
    reports = []
    if reports_dir.exists():
        reports = [
            {"name": item.name, "size": item.stat().st_size}
            for item in sorted(reports_dir.iterdir())
            if item.is_file() and item.stat().st_size > 0
        ]
    return {
        "root": str(root),
        "exists": root.exists(),
        "database": _audit_duckdb_database(root / "kpl_data.duckdb", STOCK_CORE_RELATIONS),
        "reports": reports,
        "files": _audit_project_files(
            root,
            [
                "schema.py",
                "trade_system/signals.py",
                "trade_system/risk.py",
                "trade_system/web_report.py",
                "scripts/run_integrated_daily.py",
            ],
        ),
    }


def _audit_kpl_qds(root: Path) -> dict[str, Any]:
    return {
        "root": str(root),
        "exists": root.exists(),
        "database": _audit_duckdb_database(root / "db" / "kpl_qds.duckdb"),
        "files": _audit_project_files(
            root,
            [
                "engine/signal_fusion.py",
                "engine/qstock_selector.py",
                "engine/risk_enforcer.py",
                "engine/auction_analyzer.py",
                "engine/performance_tracker.py",
                "engine/qmt_bridge.py",
                "dashboard",
                "qlib_ext/inference_pipeline.py",
                "qlib_ext/model_trainer.py",
                "config/settings.yaml",
            ],
        ),
    }


def _audit_tickflow(root: Path) -> dict[str, Any]:
    return {
        "root": str(root),
        "exists": root.exists(),
        "files": _audit_project_files(
            root,
            [
                "README.md",
                "LICENSE",
                "docs/features.md",
                "docs/strategy.md",
                "docs/custom-data-source.md",
                "backend/app/strategy/engine.py",
                "backend/app/strategy/monitor.py",
                "backend/app/backtest/engine.py",
                "backend/app/indicators/pipeline.py",
                "backend/app/data_providers/custom/provider.py",
                "frontend/src/pages/Dashboard.tsx",
            ],
        ),
    }


def _audit_vibe(root: Path) -> dict[str, Any]:
    return {
        "root": str(root),
        "exists": root.exists(),
        "files": _audit_project_files(
            root,
            [
                "README.md",
                "LICENSE",
                "backend/README.md",
                "backend/app.py",
                "backend/newsradar.py",
                "backend/portfolio.py",
                "backend/myreports.py",
                "backend/mcp_server.py",
                "backend/news_sources.json",
                "a-stock-data/SKILL.md",
                "global-stock-data/SKILL.md",
            ],
        ),
    }


def _capability_with_status(item: dict[str, str], project_roots: dict[str, Path]) -> dict[str, Any]:
    root = project_roots[item["project"]]
    status = _path_status(root, item["module_path"])
    enriched: dict[str, Any] = dict(item)
    enriched["exists"] = status["exists"]
    enriched["size"] = status["size"]
    return enriched


def audit_external_projects(
    stock_root: str | Path,
    legacy_root: str | Path,
    tickflow_root: str | Path,
    vibe_root: str | Path,
) -> dict[str, Any]:
    """Audit integration candidates without reading secrets or mutating databases."""

    stock = Path(stock_root)
    legacy = Path(legacy_root)
    tickflow = Path(tickflow_root)
    vibe = Path(vibe_root)
    project_roots = {
        "kpl_qds": legacy,
        "tickflow": tickflow,
        "vibe": vibe,
    }
    capability_matrix = [_capability_with_status(item, project_roots) for item in CAPABILITY_RULES]
    forbidden = [item for item in capability_matrix if item["decision"] == "forbid"]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "readonly": True,
        "recommended_route": "B",
        "next_phase": "Phase 12：统一数据目录与外部能力清单",
        "projects": {
            "stock_data": _audit_stock_data(stock),
            "kpl_qds": _audit_kpl_qds(legacy),
            "tickflow": _audit_tickflow(tickflow),
            "vibe": _audit_vibe(vibe),
        },
        "capability_matrix": capability_matrix,
        "forbidden_capabilities": forbidden,
        "operator_gaps": [
            "真实竞价 tick 和撤单/封单变化仍需补充。",
            "指数、个股、板块 K 线历史仍需扩容。",
            "板块资金连续性、风控快照和四阶段候选回测仍需加强。",
            "新闻、公告、研报和研究记录尚未进入统一复盘归因链路。",
        ],
    }


def _relation_rows_markdown(database: dict[str, Any], relation_type: str) -> list[str]:
    rows = []
    for name, item in sorted(database.get(relation_type, {}).items()):
        rows.append(f"| `{name}` | {item.get('row_count', 0)} |")
    return rows


def render_external_audit_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Phase 11 三项目深度审计报告",
        "",
        f"- 生成时间：`{audit['generated_at']}`",
        f"- 只读审计：`{audit['readonly']}`",
        f"- 推荐路线：`{audit['recommended_route']}`",
        f"- 下一阶段：`{audit['next_phase']}`",
        "",
        "## 职业操盘结论",
        "",
        "本阶段目标不是重做项目，而是确认哪些外部能力能以 adapter、schema、脚本、视图、报告或插件化模块进入 `stock_data`。",
        "`stock_data` 继续作为唯一主内核，外部项目只提供可验证的数据、策略、回测、投研和复盘能力。",
        "",
        "## stock_data 当前状态",
        "",
    ]
    stock = audit["projects"]["stock_data"]
    lines.extend(
        [
            f"- Root: `{stock['root']}`",
            f"- Exists: `{stock['exists']}`",
            f"- Database exists: `{stock['database']['exists']}`",
            f"- Non-empty reports: `{len(stock['reports'])}`",
            "",
            "| Core relation | Rows |",
            "|---|---:|",
        ]
    )
    rows = _relation_rows_markdown(stock["database"], "tables")
    lines.extend(rows if rows else ["| _(none)_ | 0 |"])

    for project_key, title in [
        ("kpl_qds", "A-share/kpl-qds"),
        ("tickflow", "tickflow-stock-panel"),
        ("vibe", "Vibe-Research"),
    ]:
        project = audit["projects"][project_key]
        lines.extend(["", f"## {title} 审计", "", f"- Root: `{project['root']}`", f"- Exists: `{project['exists']}`"])
        if "database" in project:
            lines.extend(
                [
                    f"- Database exists: `{project['database']['exists']}`",
                    "",
                    "| Table | Rows |",
                    "|---|---:|",
                ]
            )
            db_rows = _relation_rows_markdown(project["database"], "tables")
            lines.extend(db_rows if db_rows else ["| _(none)_ | 0 |"])
        lines.extend(["", "| File | Exists | Decision relevance |", "|---|---:|---|"])
        for path, item in sorted(project.get("files", {}).items()):
            relevance = "存在" if item["exists"] else "缺失或未克隆"
            lines.append(f"| `{path}` | `{item['exists']}` | {relevance} |")

    lines.extend(
        [
            "",
            "## 能力矩阵摘要",
            "",
            "| Capability | Project | Decision | Target | Operator value |",
            "|---|---|---|---|---|",
        ]
    )
    for item in audit["capability_matrix"]:
        lines.append(
            f"| `{item['capability_id']}` | {item['project']} | `{item['decision']}` | "
            f"`{item['target_adapter']}` | {item['operator_value']} |"
        )

    lines.extend(["", "## 禁止迁入", "", "| Capability | Reason |", "|---|---|"])
    for item in audit["forbidden_capabilities"]:
        lines.append(f"| `{item['capability_id']}` | {item['blocked_reason']} |")

    lines.extend(["", "## 当前仍缺的职业交易关键环节", ""])
    for gap in audit["operator_gaps"]:
        lines.append(f"- {gap}")

    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "进入 Phase 12：统一数据目录与外部能力清单。先建立数据源目录和 capability registry，再接 Vibe、tickflow 与 qlib shadow。",
            "",
            "## 安全边界",
            "",
            "- 本报告不读取、不复制、不输出任何 key 或 `.env` 内容。",
            "- 本报告不启用自动下单。",
            "- 本报告不修改任何 DuckDB 数据。",
            "- 本报告不复制外部项目整包。",
            "",
        ]
    )
    return "\n".join(lines)


def render_capability_matrix_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Phase 11 外部能力矩阵",
        "",
        "能力矩阵用于决定外部项目能力如何进入 `stock_data`。`port` 表示迁移思想或 adapter；`reference` 表示只参考；`forbid` 表示禁止迁入。",
        "",
        "| Capability | Project | Type | Decision | Exists | Target adapter | Blocked reason |",
        "|---|---|---|---|---:|---|---|",
    ]
    for item in audit["capability_matrix"]:
        lines.append(
            f"| `{item['capability_id']}` | {item['project']} | {item['capability_type']} | "
            f"`{item['decision']}` | `{item['exists']}` | `{item['target_adapter']}` | {item['blocked_reason']} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_phase11_outputs(
    audit: dict[str, Any],
    report_path: str | Path,
    matrix_path: str | Path,
) -> dict[str, Path]:
    report = Path(report_path)
    matrix = Path(matrix_path)
    report.parent.mkdir(parents=True, exist_ok=True)
    matrix.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_external_audit_markdown(audit), encoding="utf-8")
    matrix.write_text(render_capability_matrix_markdown(audit), encoding="utf-8")
    return {"report": report, "matrix": matrix}
