"""Conservative lifecycle classification for scripts and empty relations.

This registry is descriptive. It is used by audits and does not move or
delete files. Any archive decision still requires runtime and operator checks.
"""

from __future__ import annotations


RESEARCH_SCRIPT_TOKENS = (
    "backtest", "qlib", "strategy", "news", "ai_", "research", "screen",
)


# An endpoint probe answers only "can this route return a payload right now?".
# It does not answer whether the route belongs in the close SLA.  Keep that
# decision explicit so an empty-table report cannot turn probe success into an
# accidental request to collect every optional KPL product every day.
P1_REVIEW_TABLES = {
    "advanced_his_ranking",
    "advanced_his_sharp_withdrawal",
    "advanced_his_zhangfu_detail",
    "advanced_newhigh_group_count",
    "advanced_newhigh_group_stocks",
    "advanced_weight_performance",
    "advanced_weipan_qiangchou",
    "advanced_zhangting_expression",
    "dingpan_jijin",
    "index_info",
    "sector_bk_fenshi_zhibo",
    "sector_son_plate_direct",
    "sector_son_plates",
    "sector_sub_concepts",
}


def empty_table_collection_plan(
    table_name: str,
    classification: str,
    usefulness: str = "",
) -> dict[str, str]:
    """Return the intended collection lane for an empty relation.

    This is policy metadata, not a permission to run a network request.  The
    close pipeline consumes only its phase matrix; P1/P2 lanes are bounded
    batches with their own validation and must not become hidden daily fan-out.
    """
    if table_name == "index_info":
        return {
            "priority": "P2",
            "collection_profile": "legacy_alias",
            "cadence": "never_in_current_chain",
            "collection_reason": "当前生产写入的是 index_list，index_info 是旧表名，不应重复采集",
        }
    if table_name in {
        "holdings", "paper_order", "paper_position", "operator_trade_outcome",
        "research_note", "trade_journal", "trade_plan", "watchlist",
    }:
        return {
            "priority": "manual",
            "collection_profile": "operator_workflow",
            "cadence": "on_input",
            "collection_reason": "需要人工/外部工作流输入，不属于行情采集",
        }
    if classification in {"permission_denied", "api_error", "param_uncertain", "reachable_empty"}:
        return {
            "priority": "blocked",
            "collection_profile": "reprobe_only",
            "cadence": "on_change",
            "collection_reason": "当前不能证明可稳定写入，先修复权限/参数/时段",
        }
    if classification == "external_missing":
        return {
            "priority": "P2",
            "collection_profile": "external_dependency",
            "cadence": "on_demand",
            "collection_reason": "依赖 Qlib/财务等外部源，不由 KPL 主链采集",
        }
    if classification == "workflow_not_used":
        return {
            "priority": "manual",
            "collection_profile": "operator_workflow",
            "cadence": "on_input",
            "collection_reason": "当前没有触发对应业务工作流",
        }
    if table_name in P1_REVIEW_TABLES:
        return {
            "priority": "P1",
            "collection_profile": "review_supplement",
            "cadence": "after_close_batch",
            "collection_reason": "会增强每日复盘，但不应阻塞收盘主链",
        }
    if table_name.startswith(("advanced_", "dingpan_", "fengk_", "l2_realtime_")):
        return {
            "priority": "P2",
            "collection_profile": "professional_optional",
            "cadence": "weekly_or_on_demand",
            "collection_reason": "专业增强数据，接口虽可用但请求/语义/收益需单独验证",
        }
    if table_name.startswith(("news_", "forums_", "comments", "topic_", "tuyere_", "xianhuo_", "etf_")):
        return {
            "priority": "P2",
            "collection_profile": "professional_optional",
            "cadence": "weekly_or_on_demand",
            "collection_reason": "当前没有收盘门禁或核心复盘下游消费",
        }
    if usefulness == "professional_core":
        return {
            "priority": "P1",
            "collection_profile": "bounded_off_hours_batch",
            "cadence": "after_close_batch",
            "collection_reason": "接口可用且被标记为核心，但需先绑定下游与写入验收",
        }
    return {
        "priority": "P2",
        "collection_profile": "professional_optional",
        "cadence": "weekly_or_on_demand",
        "collection_reason": "接口探测可用，不代表应进入每日生产路径",
    }


def script_lifecycle(name: str, *, mentioned_by_runner: bool = False) -> str:
    lower = name.lower()
    if lower in {"paper_order.py", "run_daily_operator_loop.py"}:
        return "retired_reject_only"
    if lower == "run_integrated_daily.py":
        return "migration_only"
    if lower.startswith("audit_") or lower.startswith("check_"):
        return "audit_or_gate"
    if lower.startswith("backfill_") or "repair" in lower or lower.startswith("sync_"):
        return "historical_recovery"
    if any(token in lower for token in RESEARCH_SCRIPT_TOKENS):
        return "research_or_experiment"
    if lower.startswith("generate_") or lower.startswith("build_"):
        return "report_or_build"
    if lower.startswith("collect_"):
        return "production_collector" if mentioned_by_runner else "compatibility_collector"
    if lower.startswith("run_"):
        return "production_runner" if mentioned_by_runner else "compatibility_runner"
    return "support_or_unknown"


def empty_table_decision(table_name: str, classification: str, static_refs: int = 0) -> dict[str, str | int | bool]:
    """Return a reversible decision, never a destructive instruction."""
    if table_name in {
        "operator_trade_outcome", "holdings", "paper_order", "paper_position",
        "research_note", "trade_journal", "trade_plan", "watchlist",
    }:
        return {"retention": "retain_manual", "decision": "keep", "review_days": 30, "safe_to_drop": False}
    if classification == "external_missing":
        return {"retention": "retain_optional_dependency", "decision": "defer", "review_days": 90, "safe_to_drop": False}
    if classification in {"api_available_not_collected", "needs_trading_session", "reachable_empty", "api_error", "param_uncertain", "permission_denied"}:
        return {"retention": "retain_recovery_candidate", "decision": "probe_or_recover", "review_days": 30, "safe_to_drop": False}
    if classification == "workflow_not_used":
        return {"retention": "retain_workflow", "decision": "keep_until_workflow_retired", "review_days": 30, "safe_to_drop": False}
    if static_refs == 0:
        return {"retention": "orphan_candidate", "decision": "observe_then_archive", "review_days": 30, "safe_to_drop": False}
    return {"retention": "unclassified", "decision": "manual_review", "review_days": 30, "safe_to_drop": False}


def render_registry_policy() -> str:
    return """# Maintenance Registry Policy

本登记表只用于审计和分层，不自动删除文件或 DuckDB 表。

## 脚本生命周期

- `canonical_production`: 唯一日常生产入口。
- `production_collector` / `production_runner`: 被当前生产入口显式调用的链路。
- `historical_recovery`: 历史回补、缺口修复或同步工具，只能按批次运行。
- `compatibility_collector` / `compatibility_runner`: 保留用于恢复和对照，不得作为第二个定时生产写入者。
- `research_or_experiment`: QLib、回测、策略和研究链路，不进入收盘主链。
- `audit_or_gate`: 审计、检查和门禁脚本。
- `report_or_build`: 报表或构建脚本。

## 空表处理

空表先观察 30 天，检查生产引用、视图依赖、写入行为和人工工作流。只有确认无依赖后，才允许通过迁移归档；登记表中的 `safe_to_drop` 默认永远为 `false`，需要单独批准才能改变。

## 接口可用但未采集

接口探测成功只证明端点在探测参数下返回过数据。P1 复盘增强数据进入收盘后的有界批次；P2 专业数据按周或按需采集；任何一类都不自动进入收盘门禁，也不允许在每日主链中无界展开。
"""
