"""Unified data-source catalog for professional operator integrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from trade_system.integration.capability_registry import build_capability_registry_rows


DATA_SOURCE_COLUMNS = [
    "source_id",
    "source_name",
    "provider",
    "data_domain",
    "frequency",
    "auth_type",
    "requires_key",
    "coverage_start",
    "coverage_end",
    "latency_level",
    "risk_level",
    "enabled",
    "notes",
]


CAPABILITY_COLUMNS = [
    "capability_id",
    "project",
    "module_path",
    "capability_type",
    "decision",
    "target_adapter",
    "blocked_reason",
    "test_required",
    "owner_layer",
    "exists",
]


def build_default_data_sources() -> list[dict[str, Any]]:
    return [
        {
            "source_id": "kpl_api",
            "source_name": "KPL/API current stock_data source",
            "provider": "KPL",
            "data_domain": "auction",
            "frequency": "intraday/daily",
            "auth_type": "env",
            "requires_key": True,
            "coverage_start": "",
            "coverage_end": "",
            "latency_level": "near_realtime",
            "risk_level": "medium",
            "enabled": True,
            "notes": "主采集源；密钥只允许从环境读取，不写入报告。",
        },
        {
            "source_id": "legacy_qds",
            "source_name": "A-share kpl-qds legacy database",
            "provider": "local_duckdb",
            "data_domain": "sector_capital",
            "frequency": "historical",
            "auth_type": "none",
            "requires_key": False,
            "coverage_start": "",
            "coverage_end": "",
            "latency_level": "offline",
            "risk_level": "low",
            "enabled": True,
            "notes": "补充 watchlist、涨停、板块资金、龙虎榜和情绪样本。",
        },
        {
            "source_id": "stock_data_kline",
            "source_name": "stock_data native K-line tables",
            "provider": "duckdb",
            "data_domain": "kline",
            "frequency": "daily",
            "auth_type": "none",
            "requires_key": False,
            "coverage_start": "",
            "coverage_end": "",
            "latency_level": "offline",
            "risk_level": "medium",
            "enabled": True,
            "notes": "服务分阶段回测；当前指数和样本历史仍需扩容。",
        },
        {
            "source_id": "tushare_relay_basic",
            "source_name": "TuShare relay basic data supplement",
            "provider": "tushare_relay",
            "data_domain": "kline",
            "frequency": "daily/historical",
            "auth_type": "env",
            "requires_key": True,
            "coverage_start": "",
            "coverage_end": "",
            "latency_level": "delayed",
            "risk_level": "medium",
            "enabled": True,
            "notes": "补 stock_basic、trade_cal、daily、daily_basic、adj_factor、index_daily；只作为基础数据和回测补源，不直接触发交易信号。",
        },
        {
            "source_id": "eastmoney_market_flow",
            "source_name": "Eastmoney paginated market flow",
            "provider": "eastmoney_market",
            "data_domain": "stock_capital_flow",
            "frequency": "intraday/close",
            "auth_type": "public_web",
            "requires_key": False,
            "coverage_start": "2026-01-05",
            "coverage_end": "",
            "latency_level": "near_realtime",
            "risk_level": "high",
            "enabled": True,
            "notes": "Full-market paginated snapshot; one request per page and checkpointed.",
        },
        {
            "source_id": "eastmoney_sector_flow",
            "source_name": "Eastmoney sector flow",
            "provider": "eastmoney",
            "data_domain": "sector_capital_flow",
            "frequency": "intraday/close",
            "auth_type": "public_web",
            "requires_key": False,
            "coverage_start": "2026-01-05",
            "coverage_end": "",
            "latency_level": "near_realtime",
            "risk_level": "high",
            "enabled": True,
            "notes": "Direct sector flow where available; THS-derived fallback stays separately labelled.",
        },
        {
            "source_id": "ths_web_concepts",
            "source_name": "THS concept catalogue and constituents",
            "provider": "ths_web",
            "data_domain": "concept_membership",
            "frequency": "daily_snapshot",
            "auth_type": "public_web",
            "requires_key": False,
            "coverage_start": "2026-07-14",
            "coverage_end": "",
            "latency_level": "delayed",
            "risk_level": "high",
            "enabled": True,
            "notes": "Default concept source; snapshots are not historical unless date_verified.",
        },
        {
            "source_id": "sina_kline_flow",
            "source_name": "Sina K-line and flow fallback",
            "provider": "sina",
            "data_domain": "kline/stock_capital_flow",
            "frequency": "daily/on_demand",
            "auth_type": "public_web",
            "requires_key": False,
            "coverage_start": "2025-01-02",
            "coverage_end": "",
            "latency_level": "delayed",
            "risk_level": "high",
            "enabled": True,
            "notes": "Fallback only; provider_main_net is not mixed with main_orders_net without reconciliation.",
        },
        {
            "source_id": "tencent_kline_quote",
            "source_name": "Tencent K-line and quote fallback",
            "provider": "tencent",
            "data_domain": "kline/quote",
            "frequency": "intraday/daily",
            "auth_type": "public_web",
            "requires_key": False,
            "coverage_start": "2025-01-02",
            "coverage_end": "",
            "latency_level": "near_realtime",
            "risk_level": "medium",
            "enabled": True,
            "notes": "Fallback quote/K-line source with cached response provenance.",
        },
        {
            "source_id": "tickflow_custom",
            "source_name": "TickFlow custom data-source pattern",
            "provider": "adapter_contract",
            "data_domain": "strategy",
            "frequency": "daily/intraday",
            "auth_type": "env/header/query",
            "requires_key": False,
            "coverage_start": "",
            "coverage_end": "",
            "latency_level": "configurable",
            "risk_level": "medium",
            "enabled": False,
            "notes": "先迁移字段映射和降级协议，不直接依赖 tickflow 服务。",
        },
        {
            "source_id": "vibe_a_stock_data",
            "source_name": "Vibe a-stock-data research adapter",
            "provider": "public_web/adapters",
            "data_domain": "research",
            "frequency": "daily/on_demand",
            "auth_type": "none/env_optional",
            "requires_key": False,
            "coverage_start": "",
            "coverage_end": "",
            "latency_level": "delayed",
            "risk_level": "medium",
            "enabled": False,
            "notes": "补公告、研报、热点原因、资金、龙虎榜、互动问答和新闻雷达。",
        },
        {
            "source_id": "qlib_shadow",
            "source_name": "Qlib shadow predictions",
            "provider": "external_file",
            "data_domain": "ml_shadow",
            "frequency": "batch",
            "auth_type": "none",
            "requires_key": False,
            "coverage_start": "",
            "coverage_end": "",
            "latency_level": "offline",
            "risk_level": "high",
            "enabled": False,
            "notes": "只导入外部预测结果做旁路验证，默认不参与交易信号。",
        },
    ]


def _create_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS data_source_catalog (
            source_id VARCHAR PRIMARY KEY,
            source_name VARCHAR,
            provider VARCHAR,
            data_domain VARCHAR,
            frequency VARCHAR,
            auth_type VARCHAR,
            requires_key BOOLEAN,
            coverage_start VARCHAR,
            coverage_end VARCHAR,
            latency_level VARCHAR,
            risk_level VARCHAR,
            enabled BOOLEAN,
            notes VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS external_capability_registry (
            capability_id VARCHAR PRIMARY KEY,
            project VARCHAR,
            module_path VARCHAR,
            capability_type VARCHAR,
            decision VARCHAR,
            target_adapter VARCHAR,
            blocked_reason VARCHAR,
            test_required BOOLEAN,
            owner_layer VARCHAR,
            exists BOOLEAN
        )
        """
    )


def _insert_rows(con: duckdb.DuckDBPyConnection, table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    con.execute(f'DELETE FROM "{table}"')
    if not rows:
        return
    placeholders = ", ".join(["?"] * len(columns))
    column_sql = ", ".join(f'"{col}"' for col in columns)
    values = [[row.get(col) for col in columns] for row in rows]
    con.executemany(f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})', values)


def install_data_catalog(db_path: str | Path, audit: dict[str, Any]) -> dict[str, int]:
    data_sources = build_default_data_sources()
    capability_rows = build_capability_registry_rows(audit)
    con = duckdb.connect(str(db_path))
    try:
        _create_tables(con)
        _insert_rows(con, "data_source_catalog", DATA_SOURCE_COLUMNS, data_sources)
        _insert_rows(con, "external_capability_registry", CAPABILITY_COLUMNS, capability_rows)
        con.execute(
            """
            CREATE OR REPLACE VIEW v_data_source_coverage AS
            SELECT
                data_domain,
                count(*) AS source_count,
                sum(CASE WHEN enabled THEN 1 ELSE 0 END) AS enabled_count,
                string_agg(source_id, ', ' ORDER BY source_id) AS source_ids
            FROM data_source_catalog
            GROUP BY data_domain
            """
        )
        return {
            "data_source_catalog": int(con.execute("SELECT count(*) FROM data_source_catalog").fetchone()[0]),
            "external_capability_registry": int(
                con.execute("SELECT count(*) FROM external_capability_registry").fetchone()[0]
            ),
        }
    finally:
        con.close()


def render_data_source_catalog_markdown(data_sources: list[dict[str, Any]]) -> str:
    lines = [
        "# 统一数据源目录",
        "",
        "本目录定义 `stock_data` 后续可以接入的数据源。目录只描述来源、频率、权限、风险和启用状态，不保存密钥。",
        "",
        "| Source | Domain | Provider | Frequency | Auth | Enabled | Risk | Notes |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    for row in data_sources:
        lines.append(
            f"| `{row['source_id']}` | {row['data_domain']} | {row['provider']} | {row['frequency']} | "
            f"{row['auth_type']} | `{row['enabled']}` | {row['risk_level']} | {row['notes']} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_adapter_contract_markdown() -> str:
    return "\n".join(
        [
            "# Adapter Contract",
            "",
            "所有外部能力进入 `stock_data` 前必须先实现 adapter 契约。",
            "",
            "## 必填元数据",
            "",
            "- `source_id`：稳定数据源标识。",
            "- `data_domain`：auction、sector_capital、kline、research、strategy、ml_shadow 等。",
            "- `frequency`：daily、intraday、batch、on_demand。",
            "- `auth_type`：none、env、header、query 或 env_optional。",
            "- `requires_key`：是否需要密钥。",
            "- `enabled`：默认是否进入主流程。",
            "- `risk_level`：low、medium、high。",
            "",
            "## 安全约束",
            "",
            "- 不读取或输出密钥。",
            "- 不把 `.env`、settings、token、cookie 写入报告。",
            "- 不自动下单。",
            "- 不直接改写 raw 数据。",
            "- 外部数据先进入 adapter 或新增表，再经视图进入信号层。",
            "",
            "## 验证要求",
            "",
            "- 每个 adapter 必须有测试。",
            "- 每个字段映射必须能解释来源。",
            "- 失败必须降级，不得中断主日跑。",
            "- 进入信号前必须能回测或至少统计样本。",
            "",
        ]
    )
