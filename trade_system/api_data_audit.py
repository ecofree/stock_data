"""API data-source audit helpers for professional trading data coverage."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import duckdb


COMMON_ITEM_KEYS = [
    "data",
    "ticks",
    "anomalies",
    "sectors",
    "stocks",
    "plates",
    "indexes",
    "indices",
    "klines",
    "list",
    "items",
    "news",
    "themes",
    "ladder",
    "etfs",
]


@dataclass(frozen=True)
class PayloadSummary:
    response_type: str
    top_level_keys: list[str]
    item_count: int
    item_key: str


@dataclass(frozen=True)
class ApiRequirement:
    domain: str
    name: str
    endpoint: str | None
    params: dict[str, str]
    table: str | None
    importance: str
    access_method: str
    professional_use: str
    source_type: str = "api"
    fallback: str = ""


@dataclass(frozen=True)
class GapSummary:
    domain: str
    name: str
    source_type: str
    endpoint: str
    params: str
    table: str
    importance: str
    professional_use: str
    access_method: str
    http_status: str
    api_item_count: int | None
    item_key: str
    table_rows: int | None
    verdict: str
    note: str
    fallback: str


def summarize_payload(payload: Any) -> PayloadSummary:
    """Summarize a payload without exposing raw API data."""
    if payload is None:
        return PayloadSummary("none", [], 0, "")
    if isinstance(payload, list):
        return PayloadSummary("list", [], len(payload), "list")
    if isinstance(payload, dict):
        keys = sorted(str(key) for key in payload.keys())
        for key in COMMON_ITEM_KEYS:
            if key not in payload:
                continue
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                return PayloadSummary("dict", keys, len(value), key)
            if value is None:
                return PayloadSummary("dict", keys, 0, key)
            return PayloadSummary("dict", keys, 1, key)
        return PayloadSummary("dict", keys, 1 if payload else 0, "")
    return PayloadSummary(type(payload).__name__, [], 1, "")


def materialize_params(
    params: dict[str, str],
    *,
    date: str,
    stock_code: str,
    sector_code: str,
) -> dict[str, str]:
    replacements = {
        "{date}": date,
        "{stock_code}": stock_code,
        "{sector_code}": sector_code,
    }
    resolved: dict[str, str] = {}
    for key, value in params.items():
        text = str(value)
        for marker, replacement in replacements.items():
            text = text.replace(marker, replacement)
        resolved[key] = text
    return resolved


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    table_count = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0]
    view_count = con.execute(
        "SELECT count(*) FROM information_schema.views WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0]
    return bool(table_count or view_count)


def count_existing_rows(db_path: str | Path, relation: str | None) -> int | None:
    if not relation:
        return None
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if not _relation_exists(con, relation):
            return None
        return int(con.execute(f'SELECT count(*) FROM "{relation}"').fetchone()[0])
    finally:
        con.close()


def build_gap_summary(
    requirement: ApiRequirement,
    *,
    http_status: str,
    payload: PayloadSummary | None,
    table_rows: int | None,
    resolved_params: dict[str, str] | None = None,
) -> GapSummary:
    api_item_count = payload.item_count if payload else None
    item_key = payload.item_key if payload else ""
    endpoint = requirement.endpoint or ""
    params_text = urllib.parse.urlencode(resolved_params or requirement.params)
    table = requirement.table or ""

    if requirement.source_type == "api":
        if http_status == "ok" and payload and payload.item_count > 0:
            verdict = "api_available"
            note = "API probe returned structured data."
        elif http_status == "ok":
            verdict = "api_reachable_empty"
            if table_rows and table_rows > 0:
                note = (
                    "API access method appears reachable, but this probe returned empty data; "
                    "local table already has rows, so coverage may be date/session/sample dependent."
                )
            else:
                note = "API access method appears reachable, but this probe returned empty data."
        elif http_status == "missing_key":
            verdict = "api_not_configured"
            note = "API key is not configured."
        elif http_status == "not_probed":
            verdict = "api_not_probed"
            note = "Live API probe was skipped."
        else:
            verdict = "api_error"
            note = f"API probe failed with status {http_status}."
    elif requirement.source_type == "derived":
        if table_rows and table_rows > 0:
            verdict = "derived_available"
            note = "Data exists locally but is derived/fallback, not a dedicated API feed."
        else:
            verdict = "derived_missing"
            note = "Derived/fallback data is not populated."
    elif requirement.source_type == "external_file":
        if table_rows and table_rows > 0:
            verdict = "external_loaded"
            note = "External/shadow data is loaded locally."
        else:
            verdict = "external_missing"
            note = "Requires an external file/import job; not obtainable from the current API."
    else:
        if table_rows and table_rows > 0:
            verdict = "local_available"
            note = "Local/manual/research data exists; not a direct API feed."
        else:
            verdict = "local_missing"
            note = "Local/manual/research data is missing; not a direct API feed."

    return GapSummary(
        domain=requirement.domain,
        name=requirement.name,
        source_type=requirement.source_type,
        endpoint=endpoint,
        params=params_text,
        table=table,
        importance=requirement.importance,
        professional_use=requirement.professional_use,
        access_method=requirement.access_method,
        http_status=http_status,
        api_item_count=api_item_count,
        item_key=item_key,
        table_rows=table_rows,
        verdict=verdict,
        note=note,
        fallback=requirement.fallback,
    )


def safe_probe_json(
    *,
    api_base: str,
    api_key: str,
    endpoint: str,
    params: dict[str, str],
    timeout: int = 12,
) -> tuple[str, PayloadSummary | None]:
    key = (api_key or "").strip()
    if not key or key == "replace-me":
        return "missing_key", None
    url = f"{api_base.rstrip('/')}{endpoint}"
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"accept": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return "ok", summarize_payload(payload)
    except urllib.error.HTTPError as exc:
        return f"http_{exc.code}", None
    except json.JSONDecodeError:
        return "invalid_json", None
    except TimeoutError:
        return "timeout", None
    except Exception as exc:
        return f"error_{type(exc).__name__}", None


def build_default_requirements() -> list[ApiRequirement]:
    """Professional trading data needs mapped to current stock_data access paths."""
    return [
        ApiRequirement("market_state", "daily_summary", "/daily", {"date": "{date}"}, "daily_summary", "core", "GET /daily?date=<YYYY-MM-DD>", "daily market breadth and limit-up/down context"),
        ApiRequirement("market_state", "daily_sentiment", "/daily/sentiment", {"date": "{date}"}, "daily_sentiment", "core", "GET /daily/sentiment?date=<YYYY-MM-DD>", "sentiment-cycle scoring"),
        ApiRequirement("market_state", "daily_new_high", "/daily/new-high", {"date": "{date}"}, "daily_new_high", "important", "GET /daily/new-high?date=<YYYY-MM-DD>", "new-high breadth and risk appetite"),
        ApiRequirement("market_state", "daily_export", "/daily/export", {"date": "{date}"}, "daily_export", "optional", "GET /daily/export?date=<YYYY-MM-DD>", "raw daily export snapshot"),
        ApiRequirement("market_state", "market_rise_fall", "/market/rise-fall", {"date": "{date}"}, "market_rise_fall", "core", "GET /market/rise-fall?date=<YYYY-MM-DD>", "rise/fall distribution and limit-pool pressure"),
        ApiRequirement("market_state", "market_mood", "/market/mood", {}, "market_mood", "important", "GET /market/mood", "current market mood fallback"),
        ApiRequirement("market_state", "market_limit_up_down", "/market/limit-up-down", {}, "market_limit_up_down", "core", "GET /market/limit-up-down", "limit-up/down stock pool"),
        ApiRequirement("limit_pool", "ladder_market", "/ladder/market", {"date": "{date}"}, "ladder_market", "core", "GET /ladder/market?date=<YYYY-MM-DD>", "board ladder and height"),
        ApiRequirement("limit_pool", "ladder_realtime_boards", "/ladder/realtime-boards", {}, "ladder_realtime_boards", "important", "GET /ladder/realtime-boards", "intraday board formation"),
        ApiRequirement("limit_pool", "l2_realtime_all_boards", "/l2/realtime/all-boards", {}, "l2_realtime_all_boards", "core", "GET /l2/realtime/all-boards", "realtime board pool for intraday monitoring"),
        ApiRequirement("sector_theme", "sector_plates", "/sector/plates", {}, "sector_plates", "core", "GET /sector/plates", "sector universe"),
        ApiRequirement("sector_theme", "sector_ranking", "/sector/ranking", {"date": "{date}"}, "sector_ranking", "core", "GET /sector/ranking?date=<YYYY-MM-DD>", "theme/mainline ranking"),
        ApiRequirement("sector_theme", "sector_strength", "/sector/strength", {"code": "{sector_code}", "date": "{date}"}, "sector_strength", "core", "GET /sector/strength?code=<sector_code>&date=<YYYY-MM-DD>", "sector strength and seal-rate evidence"),
        ApiRequirement("sector_theme", "sector_stocks", "/sector/stocks", {"code": "{sector_code}", "date": "{date}"}, "sector_stocks", "core", "GET /sector/stocks?code=<sector_code>&date=<YYYY-MM-DD>", "constituents for candidate expansion"),
        ApiRequirement("sector_theme", "sector_capital", "/sector/capital", {"code": "{sector_code}", "date": "{date}"}, "sector_capital", "core", "GET /sector/capital?code=<sector_code>&date=<YYYY-MM-DD>", "sector money flow and mainline confirmation"),
        ApiRequirement("sector_theme", "sector_intraday", "/l2/sector-intraday", {"code": "{sector_code}", "date": "{date}"}, "l2_sector_intraday", "important", "GET /l2/sector-intraday?code=<sector_code>&date=<YYYY-MM-DD>", "sector intraday strength curve"),
        ApiRequirement("sector_theme", "theme_hot", "/theme/hot", {}, "theme_hot", "important", "GET /theme/hot", "hot theme fallback"),
        ApiRequirement("auction", "advanced_morning_bidding_summary", "/advanced/morning-bidding-summary", {}, "advanced_morning_bidding_summary", "core", "GET /advanced/morning-bidding-summary", "auction-market aggregate strength"),
        ApiRequirement("auction", "advanced_morning_bidding_list", "/advanced/morning-bidding-list", {}, "advanced_morning_bidding_list", "core", "GET /advanced/morning-bidding-list", "stock-level auction pool"),
        ApiRequirement("auction", "auction_tick", "/auction/tick", {"code": "{stock_code}", "date": "{date}"}, "auction_tick", "core", "GET /auction/tick?code=<stock_code>&date=<YYYY-MM-DD>", "09:15-09:25 auction confirmation", fallback="Use auction_bidding_anomaly and advanced_morning_bidding_summary until ticks are non-empty."),
        ApiRequirement("auction", "auction_bidding_anomaly", "/auction/bidding-anomaly", {"code": "{stock_code}", "date": "{date}"}, "auction_bidding_anomaly", "core", "GET /auction/bidding-anomaly?code=<stock_code>&date=<YYYY-MM-DD>", "auction anomaly and large-order confirmation"),
        ApiRequirement("kline", "stock_kline", "/kline", {"code": "{stock_code}", "ktype": "d", "count": "5"}, "kline", "core", "GET /kline?code=<stock_code>&ktype=d&count=5", "candidate filters and staged backtest"),
        ApiRequirement("index", "l2_realtime_index_list", "/l2/realtime/index-list", {}, "l2_realtime_index_list", "important", "GET /l2/realtime/index-list", "index intraday risk context"),
        ApiRequirement("index", "index_kline", None, {}, "index_kline", "core", "derived from /daily raw_json", "index trend filter for market-state risk", source_type="derived", fallback="/daily exposes close/change/turnover only; open/high/low remain missing."),
        ApiRequirement("l2_orderflow", "l2_stock_intraday", "/l2/stock-intraday", {"code": "{stock_code}", "date": "{date}"}, "l2_stock_intraday", "important", "GET /l2/stock-intraday?code=<stock_code>&date=<YYYY-MM-DD>", "intraday price/volume/main-fund curve"),
        ApiRequirement("l2_orderflow", "l2_stock_bigorder", "/l2/stock-bigorder", {"code": "{stock_code}", "date": "{date}"}, "l2_stock_bigorder", "important", "GET /l2/stock-bigorder?code=<stock_code>&date=<YYYY-MM-DD>", "big-order confirmation"),
        ApiRequirement("l2_orderflow", "advanced_main_monitor", "/advanced/main-monitor", {"code": "{stock_code}"}, "advanced_main_monitor", "important", "GET /advanced/main-monitor?code=<stock_code>", "main-fund monitor evidence for intraday strength"),
        ApiRequirement("l2_orderflow", "advanced_zjmm_min", "/advanced/zjmm-min", {"code": "{stock_code}", "date": "{date}"}, "advanced_zjmm_min", "important", "GET /advanced/zjmm-min?code=<stock_code>&date=<YYYY-MM-DD>", "minute-level main-money flow"),
        ApiRequirement("l2_orderflow", "advanced_dadan_kline", "/advanced/dadan-kline", {"code": "{stock_code}"}, "advanced_dadan_kline", "important", "GET /advanced/dadan-kline?code=<stock_code>", "large-order K-line evidence"),
        ApiRequirement("l2_orderflow", "advanced_main_activity_kline", "/advanced/main-activity-kline", {"code": "{stock_code}"}, "advanced_main_activity_kline", "important", "GET /advanced/main-activity-kline?code=<stock_code>", "main-activity K-line evidence"),
        ApiRequirement("l2_orderflow", "advanced_pankou", "/advanced/pankou", {"code": "{stock_code}"}, "advanced_pankou", "important", "GET /advanced/pankou?code=<stock_code>", "盘口辅助证据"),
        ApiRequirement("dragon_tiger", "lhb_list", "/lhb/list", {"date": "{date}"}, "lhb_list", "important", "GET /lhb/list?date=<YYYY-MM-DD>", "hot-money and post-trade review"),
        ApiRequirement("dragon_tiger", "lhb_detail", "/lhb/detail", {"code": "{stock_code}", "date": "{date}"}, "lhb_detail", "optional", "GET /lhb/detail?code=<stock_code>&date=<YYYY-MM-DD>", "stock-level LHB attribution"),
        ApiRequirement("news_research", "advanced_news_flash", "/advanced/news-flash", {}, "advanced_news_flash", "important", "GET /advanced/news-flash", "intraday catalyst radar"),
        ApiRequirement("news_research", "news_columns", "/news/columns", {}, "news_columns", "optional", "GET /news/columns", "news taxonomy"),
        ApiRequirement("news_research", "news_theme", "/news/theme", {"type": "-1", "index": "0", "page_size": "20"}, "news_theme", "important", "GET /news/theme?type=-1&index=0&page_size=20", "theme/news catalyst pool"),
        ApiRequirement("stock_profile", "stock_company_info", "/stock/company-info", {"code": "{stock_code}"}, "stock_company_info", "important", "GET /stock/company-info?code=<stock_code>", "F10/company profile"),
        ApiRequirement("stock_profile", "stock_institutional_positions", "/stock/institutional-positions", {"code": "{stock_code}"}, "stock_institutional_positions", "optional", "GET /stock/institutional-positions?code=<stock_code>", "institutional holding context"),
        ApiRequirement("research_layer", "news_radar_item", None, {}, "news_radar_item", "important", "local Vibe-style news radar import", "catalyst tagging and AI review context", source_type="local"),
        ApiRequirement("research_layer", "research_note", None, {}, "research_note", "important", "manual/local research-note table", "operator research notes and review memory", source_type="local"),
        ApiRequirement("research_layer", "research_report_file", None, {}, "research_report_file", "optional", "manual/local research-report registry", "report and announcement management", source_type="local"),
        ApiRequirement("ml_shadow", "qlib_prediction", None, {}, "qlib_prediction", "optional", "external CSV via scripts/import_qlib_shadow_predictions.py", "shadow-mode factor/model validation", source_type="external_file", fallback="Do not use as direct trading signal until evaluated."),
    ]


def audit_requirements(
    *,
    db_path: str | Path,
    api_base: str,
    api_key: str,
    date: str,
    stock_code: str,
    sector_code: str,
    live_probe: bool = True,
    timeout: int = 12,
    requirements: list[ApiRequirement] | None = None,
) -> list[GapSummary]:
    results: list[GapSummary] = []
    for requirement in requirements or build_default_requirements():
        resolved = materialize_params(
            requirement.params,
            date=date,
            stock_code=stock_code,
            sector_code=sector_code,
        )
        if live_probe and requirement.endpoint and requirement.source_type == "api":
            http_status, payload = safe_probe_json(
                api_base=api_base,
                api_key=api_key,
                endpoint=requirement.endpoint,
                params=resolved,
                timeout=timeout,
            )
        else:
            http_status, payload = "not_probed", None
        table_rows = count_existing_rows(db_path, requirement.table)
        results.append(
            build_gap_summary(
                requirement,
                http_status=http_status,
                payload=payload,
                table_rows=table_rows,
                resolved_params=resolved,
            )
        )
    return results


def _fmt_count(value: int | None) -> str:
    return "missing" if value is None else str(value)


def _markdown_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_No rows._"]
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    for row in rows[1:]:
        escaped = [cell.replace("\n", " ").replace("|", "/") for cell in row]
        lines.append("| " + " | ".join(escaped) + " |")
    return lines


def render_api_data_audit_report(
    summaries: list[GapSummary],
    *,
    date: str,
    stock_code: str,
    sector_code: str,
    live_probe: bool,
) -> str:
    counts = Counter(summary.verdict for summary in summaries)
    lines = [
        "# API 数据能力与缺口审计",
        "",
        "## 审计范围",
        "",
        f"- 交易日: `{date}`",
        f"- 样本股票: `{stock_code}`",
        f"- 样本板块: `{sector_code}`",
        f"- 是否真实探测 API: `{str(live_probe).lower()}`",
        "- 安全约束: 报告只记录 endpoint、参数名/样本、状态、字段数量和本地行数，不输出 API key 或原始大段数据。",
        "",
        "## 结论摘要",
        "",
    ]
    for verdict, count in sorted(counts.items()):
        lines.append(f"- `{verdict}`: {count}")

    core_missing = [
        s
        for s in summaries
        if s.importance == "core"
        and s.verdict
        in {"api_reachable_empty", "api_error", "api_not_configured", "derived_missing", "local_missing", "external_missing"}
    ]
    lines.extend(["", "## 职业操盘关键缺口", ""])
    if core_missing:
        for item in core_missing:
            lines.append(
                f"- `{item.name}` ({item.domain}): {item.verdict}; "
                f"table_rows={_fmt_count(item.table_rows)}; {item.note}"
            )
            if item.fallback:
                lines.append(f"  fallback: {item.fallback}")
    else:
        lines.append("- No core gaps detected by this bounded audit.")

    groups = [
        ("可由当前 API 获取且本次探测有数据", {"api_available"}),
        ("API 接入方式可达但本次返回空", {"api_reachable_empty"}),
        ("API 错误或未配置", {"api_error", "api_not_configured"}),
        ("非 API/派生/本地/外部文件数据", {"derived_available", "derived_missing", "local_available", "local_missing", "external_loaded", "external_missing"}),
    ]
    for title, verdicts in groups:
        lines.extend(["", f"## {title}", ""])
        group_rows = [["Domain", "Data", "Endpoint/Source", "Probe", "API Count", "Table Rows", "Use", "Note"]]
        for item in summaries:
            if item.verdict not in verdicts:
                continue
            group_rows.append(
                [
                    item.domain,
                    item.name,
                    item.endpoint or item.access_method,
                    item.http_status,
                    "" if item.api_item_count is None else str(item.api_item_count),
                    _fmt_count(item.table_rows),
                    item.professional_use,
                    item.note,
                ]
            )
        lines.extend(_markdown_table(group_rows))

    lines.extend(["", "## 全量数据矩阵", ""])
    matrix_rows = [["Domain", "Name", "Type", "Endpoint", "Params", "Table", "Rows", "Verdict", "Access Method"]]
    for item in summaries:
        matrix_rows.append(
            [
                item.domain,
                item.name,
                item.source_type,
                item.endpoint or "",
                item.params,
                item.table,
                _fmt_count(item.table_rows),
                item.verdict,
                item.access_method,
            ]
        )
    lines.extend(_markdown_table(matrix_rows))

    lines.extend(
        [
            "",
            "## 操盘视角判断",
            "",
            "- `auction_tick` 若持续为 `api_reachable_empty`，不能伪造逐笔竞价，只能把竞价异动、早盘竞价汇总、L2 与盘口作为弱替代证据。",
            "- `index_kline` 当前是 `/daily` 派生 fallback，能服务市场方向过滤，但不能做严格指数 OHLC 回测。",
            "- 新闻、研究记录、研报、qlib shadow 不是当前 KPL API 的直接数据，应通过本地导入/适配器进入，不应混入主交易信号。",
            "- 候选股四阶段信号要优先依赖已验证有数的 K 线、板块强度/资金、涨停梯队、L2/大单和竞价异常；缺口数据必须在信号证据字段里明确降级。",
            "",
        ]
    )
    return "\n".join(lines)
