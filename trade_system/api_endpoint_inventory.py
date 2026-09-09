"""Full KPL endpoint inventory extraction, probing, and reporting."""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
import time
import urllib.parse

import duckdb

from trade_system.api_data_audit import safe_probe_json


API_PREFIXES = {
    "advanced",
    "auction",
    "comments",
    "daily",
    "dingpan",
    "etf",
    "fengk",
    "finance",
    "forums",
    "index",
    "kline",
    "l2",
    "ladder",
    "lhb",
    "main-force",
    "market",
    "news",
    "sector",
    "stock",
    "theme",
    "topic",
    "tuyere",
    "xianhuo",
}


ENDPOINT_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_./{}*?=&:-]*$")


TABLE_ENDPOINT_EXCEPTIONS = {
    "daily_summary": "/daily",
    "daily_new_high": "/daily/new-high",
    "daily_sentiment": "/daily/sentiment",
    "daily_export": "/daily/export",
    "index_kline": "/index/zhishu-kline",
    "index_info": "/index/list",
    "l2_realtime_all_boards": "/l2/realtime/all-boards",
    "l2_realtime_index_list": "/l2/realtime/index-list",
    "l2_realtime_index_trend": "/l2/realtime/index-trend",
    "l2_realtime_sharp_withdrawal": "/l2/realtime/sharp-withdrawal",
    "market_emotion_money": "/market/emotion-money-date",
    "market_emotion_detail": "/market/emotion-money-detail",
    "comments": "/comments",
    "kline": "/kline",
    "collect_log": None,
}


CODE_TO_SECTOR_ENDPOINTS = {
    "/sector/strength",
    "/sector/all-stocks",
    "/sector/stocks",
    "/sector/capital",
    "/sector/boom-reason",
    "/sector/son-plates",
    "/sector/sub-concepts",
    "/sector/son-plate-direct",
    "/sector/parent-plate",
    "/sector/plate-info-qj",
    "/sector/bk-fenshi-zhibo",
    "/l2/sector-intraday",
    "/l2/sector-volume",
}


DATE_ENDPOINTS = {
    "/daily",
    "/daily/export",
    "/daily/new-high",
    "/daily/sentiment",
    "/etf/all",
    "/etf/ranking",
    "/index/list",
    "/ladder/board-stocks",
    "/ladder/broken",
    "/ladder/consecutive",
    "/ladder/market",
    "/ladder/sector",
    "/ladder/sharp-withdrawal",
    "/lhb/dataframe",
    "/lhb/list",
    "/lhb/raw-list",
    "/lhb/youzi-dongxiang",
    "/market/rise-fall",
    "/sector/ranking",
}


NO_PARAM_ENDPOINTS = {
    "/advanced/business-list",
    "/advanced/concept-point",
    "/advanced/fengk-best",
    "/advanced/market-mood-count",
    "/advanced/market-radar",
    "/advanced/morning-bidding-list",
    "/advanced/morning-bidding-summary",
    "/advanced/news-flash",
    "/advanced/news-flash-top",
    "/advanced/on-the-lhb",
    "/advanced/pianlizhi",
    "/advanced/relation",
    "/advanced/weipan-qiangchou",
    "/dingpan/all",
    "/dingpan/art-title",
    "/dingpan/jijin",
    "/dingpan/module-versatile",
    "/dingpan/northbound-close-date",
    "/dingpan/southbound-close-date",
    "/dingpan/weipan",
    "/l2/realtime/all-boards",
    "/l2/realtime/index-list",
    "/l2/realtime/index-trend",
    "/l2/realtime/sharp-withdrawal",
    "/ladder/realtime-boards",
    "/lhb/top-title",
    "/lhb/update-list",
    "/market/emotion-money-date",
    "/market/emotion-money-detail",
    "/market/limit-up-down",
    "/market/mood",
    "/news/columns",
    "/news/concept-jxbk",
    "/news/index-plate",
    "/sector/plates",
    "/theme/hot",
    "/xianhuo/list",
}


RANGE_ENDPOINTS = {
    "/advanced/interviews": {"start", "end"},
    "/sector/strength-dataframe": {"code", "start", "end"},
}


BATCH_ENDPOINTS = {
    "/advanced/company-count": {"codes"},
    "/sector/strength-batch": {"codes", "date"},
}


PAGINATION_ENDPOINTS = {
    "/fengk/list": {"index", "page_size", "date"},
    "/news/theme": {"type", "index", "page_size"},
    "/topic/list": {"index", "page_size"},
    "/forums/sel-list": {"index", "page_size"},
}


SPECIAL_PARAMS = {
    "/advanced/holiday": {"year"},
    "/advanced/newhigh-group-stocks": {"group_type"},
    "/topic/detail": {"topic_id"},
    "/topic/vote": {"topic_id"},
    "/sector/strength-ndays": {"code", "end_date", "days"},
    "/auction/bidding-anomaly": {"code", "date"},
    "/auction/tick": {"code", "date"},
    "/auction/market": {"date"},
    "/kline": {"code", "ktype", "count"},
    "/lhb/detail": {"code", "date"},
    "/index/zhishu-kline": {"code", "ktype", "index"},
    "/dingpan/radar": {"st"},
}


REALTIME_HINTS = (
    "realtime",
    "intraday",
    "tick",
    "morning-bidding",
    "auction",
    "weipan",
    "radar",
    "pankou",
    "dp-realdata",
    "zs-real",
)


CORE_PREFIXES = {"daily", "market", "sector", "auction", "ladder", "l2", "lhb"}


@dataclass
class EndpointCandidate:
    endpoint: str
    source_kind: str
    source_file: str
    param_names: set[str] = field(default_factory=set)
    table_name: str | None = None


@dataclass
class EndpointInventoryItem:
    endpoint: str
    category: str
    source_kinds: list[str]
    source_files: list[str]
    param_names: list[str]
    param_type: str
    probe_params: dict[str, str]
    table_name: str | None = None
    table_rows: int | None = None
    probe_status: str = "not_probed"
    response_type: str = ""
    item_key: str = ""
    item_count: int | None = None
    top_level_keys: list[str] = field(default_factory=list)
    verdict: str = "not_probed"
    usefulness: str = "low"
    note: str = ""
    probed_at: str = ""


def normalize_endpoint(value: str) -> str | None:
    text = str(value or "").strip().strip("`'\"")
    if not text.startswith("/"):
        return None
    text = text.split("#", 1)[0].split("?", 1)[0].rstrip(".,;，。；)|")
    if not ENDPOINT_RE.match(text):
        return None
    first = text.strip("/").split("/", 1)[0]
    if first not in API_PREFIXES:
        return None
    if "*" in text:
        return None
    return text


def classify_param_type(param_names: list[str] | set[str]) -> str:
    names = set(param_names)
    if not names:
        return "none"
    if {"index", "page_size"} & names:
        return "pagination"
    if "codes" in names:
        return "batch"
    if {"start", "end"} <= names:
        return "range"
    if "end_date" in names:
        return "range"
    if "plate" in names:
        return "code_date" if "date" in names else "code"
    if names == {"date"}:
        return "date"
    if names == {"code"}:
        return "code"
    if names == {"code", "date"}:
        return "code_date"
    if "code" in names and "date" in names:
        return "code_date"
    if "code" in names:
        return "code"
    if "date" in names:
        return "date"
    return "combination"


def table_to_endpoint(table_name: str) -> str | None:
    if table_name in TABLE_ENDPOINT_EXCEPTIONS:
        return TABLE_ENDPOINT_EXCEPTIONS[table_name]
    if "_" not in table_name:
        return None
    prefix, suffix = table_name.split("_", 1)
    if prefix not in API_PREFIXES:
        return None
    return f"/{prefix}/{suffix.replace('_', '-')}"


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dict_param_names(node: ast.AST | None) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    names = set()
    for key in node.keys:
        text = _literal_string(key) if key is not None else None
        if text:
            names.add(text)
    return names


def extract_python_candidates(path: str | Path) -> list[EndpointCandidate]:
    file_path = Path(path)
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []

    candidates: list[EndpointCandidate] = []
    endpoint_table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if attr == "log_collect" and len(node.args) >= 2:
                table = _literal_string(node.args[0])
                endpoint = normalize_endpoint(_literal_string(node.args[1]) or "")
                if endpoint and table:
                    endpoint_table[endpoint] = table

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if attr == "get" and node.args:
                endpoint = normalize_endpoint(_literal_string(node.args[0]) or "")
                if endpoint:
                    params = _dict_param_names(node.args[1] if len(node.args) > 1 else None)
                    candidates.append(
                        EndpointCandidate(
                            endpoint=endpoint,
                            source_kind="code",
                            source_file=str(file_path),
                            param_names=params,
                            table_name=endpoint_table.get(endpoint),
                        )
                    )
            elif attr in {"insert_raw"} and node.args:
                endpoint = normalize_endpoint(_literal_string(node.args[0]) or "")
                if endpoint:
                    candidates.append(EndpointCandidate(endpoint, "code", str(file_path)))

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            endpoint = normalize_endpoint(node.value)
            if endpoint:
                candidates.append(EndpointCandidate(endpoint, "code", str(file_path), table_name=endpoint_table.get(endpoint)))
    return candidates


def extract_markdown_candidates(path: str | Path) -> list[EndpointCandidate]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    candidates = []
    for raw in re.findall(r"`(/[^`\s<，。；,|)]*)`", text):
        endpoint = normalize_endpoint(raw)
        if endpoint:
            candidates.append(EndpointCandidate(endpoint, "doc", str(file_path)))
    return candidates


def extract_schema_candidates(path: str | Path) -> list[EndpointCandidate]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    candidates = []
    for table_name in re.findall(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z0-9_]+)", text):
        endpoint = normalize_endpoint(table_to_endpoint(table_name) or "")
        if endpoint:
            candidates.append(EndpointCandidate(endpoint, "schema", str(file_path), table_name=table_name))
    return candidates


def infer_param_names(endpoint: str, existing: set[str]) -> set[str]:
    if existing:
        return set(existing)
    if endpoint in NO_PARAM_ENDPOINTS:
        return set()
    if endpoint in DATE_ENDPOINTS:
        return {"date"}
    if endpoint in SPECIAL_PARAMS:
        return set(SPECIAL_PARAMS[endpoint])
    if endpoint in RANGE_ENDPOINTS:
        return set(RANGE_ENDPOINTS[endpoint])
    if endpoint in BATCH_ENDPOINTS:
        return set(BATCH_ENDPOINTS[endpoint])
    if endpoint in PAGINATION_ENDPOINTS:
        return set(PAGINATION_ENDPOINTS[endpoint])
    if endpoint in CODE_TO_SECTOR_ENDPOINTS:
        return {"code", "date"} if endpoint in {"/sector/strength", "/sector/stocks", "/sector/capital", "/sector/boom-reason", "/sector/bk-fenshi-zhibo", "/l2/sector-intraday"} else {"code"}
    if endpoint.startswith("/stock/") or endpoint.startswith("/tuyere/"):
        return {"code"}
    if endpoint.startswith("/finance/"):
        return {"code"}
    if endpoint.startswith("/l2/stock-") or endpoint.startswith("/l2/tick-"):
        return {"code", "date"}
    if endpoint in {"/advanced/trend-min", "/advanced/zjmm-min"}:
        return {"code", "date"}
    if endpoint in {
        "/advanced/corporate-news",
        "/advanced/dadan-trend-incremental",
        "/advanced/fenbi2",
        "/advanced/stock-plate-new",
        "/advanced/stock-trend-incremental-ph",
        "/advanced/turnover-ten",
        "/advanced/zhangting-gene",
    }:
        return {"code"}
    if endpoint.startswith("/advanced/kline-today") or endpoint.startswith("/advanced/dadan-kline-today"):
        return {"code", "ktype"}
    if endpoint.startswith("/advanced/") and (
        "kline" in endpoint
        or endpoint.endswith(("/chouma", "/main-monitor", "/pankou", "/gudong-info", "/gudong-renshu", "/rqz-data", "/vol-tur"))
    ):
        return {"code"}
    if endpoint.startswith("/index/"):
        return {"date"} if endpoint == "/index/list" else {"code", "date"}
    return set()


def build_probe_params(
    param_names: list[str],
    *,
    date: str,
    stock_code: str,
    sector_code: str,
) -> dict[str, str]:
    params = {}
    start = "2026-06-01" if date >= "2026-06-01" else date
    for name in param_names:
        if name == "date":
            params[name] = date
        elif name == "start":
            params[name] = start
        elif name == "end":
            params[name] = date
        elif name == "end_date":
            params[name] = date
        elif name == "days":
            params[name] = "5"
        elif name == "year":
            params[name] = date[:4]
        elif name == "code":
            params[name] = sector_code if name == "code" and False else stock_code
        elif name == "codes":
            params[name] = f"{sector_code},{stock_code}"
        elif name == "group_type":
            params[name] = "all"
        elif name == "ktype":
            params[name] = "d"
        elif name == "count":
            params[name] = "5"
        elif name == "index":
            params[name] = "0"
        elif name == "page_size":
            params[name] = "20"
        elif name == "type":
            params[name] = "-1"
        elif name == "st":
            params[name] = "0"
        elif name == "plate":
            params[name] = sector_code
        elif name == "topic_id":
            params[name] = "1"
        else:
            params[name] = ""
    return params


def _category(endpoint: str) -> str:
    return endpoint.strip("/").split("/", 1)[0]


def _usefulness(endpoint: str) -> str:
    category = _category(endpoint)
    if category in CORE_PREFIXES:
        return "professional_core"
    if category in {"advanced", "dingpan", "fengk", "news", "stock", "tuyere", "theme"}:
        return "professional_useful"
    if category in {"finance", "etf", "xianhuo", "forums", "comments", "topic"}:
        return "low_priority"
    return "reference"


def merge_candidates(candidates: list[EndpointCandidate]) -> list[EndpointInventoryItem]:
    grouped: dict[str, list[EndpointCandidate]] = defaultdict(list)
    for candidate in candidates:
        endpoint = normalize_endpoint(candidate.endpoint)
        if endpoint:
            grouped[endpoint].append(candidate)

    items: list[EndpointInventoryItem] = []
    for endpoint, group in grouped.items():
        params: set[str] = set()
        table_name = None
        source_kinds = set()
        source_files = set()
        for candidate in group:
            params.update(candidate.param_names)
            source_kinds.add(candidate.source_kind)
            source_files.add(candidate.source_file)
            if candidate.table_name and not table_name:
                table_name = candidate.table_name
        params = infer_param_names(endpoint, params)
        param_list = sorted(params)
        items.append(
            EndpointInventoryItem(
                endpoint=endpoint,
                category=_category(endpoint),
                source_kinds=sorted(source_kinds),
                source_files=sorted(source_files),
                param_names=param_list,
                param_type=classify_param_type(param_list),
                probe_params={},
                table_name=table_name,
                usefulness=_usefulness(endpoint),
            )
        )
    return sorted(items, key=lambda item: item.endpoint)


def build_inventory_from_project(project_root: str | Path) -> list[EndpointInventoryItem]:
    root = Path(project_root)
    candidates: list[EndpointCandidate] = []
    for path in sorted(root.glob("collect_*.py")):
        candidates.extend(extract_python_candidates(path))
    # ``fill_final.py`` was a hard-coded, exception-swallowing one-off runner
    # and is intentionally retired.  The staged/backfill scripts own history
    # now; inventory only needs to inspect the supported market entrypoint.
    for path in [root / "fetch_all.py"]:
        candidates.extend(extract_python_candidates(path))
    candidates.extend(extract_schema_candidates(root / "schema.py"))
    for path in [root / "trading_data_application.md", *sorted((root / "docs").rglob("*.md"))]:
        if path.name in {"api_data_gap_matrix.md", "api_data_source_audit_latest.md"}:
            continue
        candidates.extend(extract_markdown_candidates(path))
    return merge_candidates(candidates)


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


def attach_table_counts(items: list[EndpointInventoryItem], db_path: str | Path) -> None:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for item in items:
            if item.table_name and _relation_exists(con, item.table_name):
                item.table_rows = int(con.execute(f'SELECT count(*) FROM "{item.table_name}"').fetchone()[0])
    finally:
        con.close()


def _needs_sector_code(endpoint: str) -> bool:
    return endpoint in CODE_TO_SECTOR_ENDPOINTS or endpoint.startswith("/sector/")


def probe_inventory(
    items: list[EndpointInventoryItem],
    *,
    api_base: str,
    api_key: str,
    date: str,
    stock_code: str,
    sector_code: str,
    timeout: int = 8,
    delay: float = 0.12,
    max_endpoints: int | None = None,
) -> None:
    selected = items if max_endpoints is None else items[:max_endpoints]
    for item in selected:
        effective_stock = sector_code if _needs_sector_code(item.endpoint) else stock_code
        item.probe_params = build_probe_params(
            item.param_names,
            date=date,
            stock_code=effective_stock,
            sector_code=sector_code,
        )
        if item.param_type == "combination" and any(value == "" for value in item.probe_params.values()):
            item.probe_status = "not_probed"
            item.verdict = "param_uncertain"
            item.note = "Parameter shape is not fully inferred."
            continue
        status, summary = safe_probe_json(
            api_base=api_base,
            api_key=api_key,
            endpoint=item.endpoint,
            params=item.probe_params,
            timeout=timeout,
        )
        item.probe_status = status
        item.probed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if summary:
            item.response_type = summary.response_type
            item.item_key = summary.item_key
            item.item_count = summary.item_count
            item.top_level_keys = summary.top_level_keys[:12]
        item.verdict, item.note = classify_probe_result(item)
        if delay:
            time.sleep(delay)


def classify_probe_result(item: EndpointInventoryItem) -> tuple[str, str]:
    if item.probe_status == "missing_key":
        return "api_not_configured", "API key is missing."
    if item.probe_status == "not_probed":
        return item.verdict, item.note or "Endpoint was not probed."
    if item.probe_status == "http_422":
        return "param_uncertain", "Endpoint exists but rejected the inferred parameter shape."
    if item.probe_status != "ok":
        if item.param_type in {"combination"}:
            return "param_uncertain", f"Probe failed ({item.probe_status}); parameter shape may be uncertain."
        return "api_error", f"Probe failed with {item.probe_status}."
    if item.item_count and item.item_count > 0:
        if item.table_rows and item.table_rows > 0:
            return "stable_available", "Probe returned data and local table has stored rows."
        return "api_available", "Probe returned data; local table is not populated or not mapped."
    if any(hint in item.endpoint for hint in REALTIME_HINTS):
        return "needs_trading_session", "Endpoint is reachable but empty; likely trading-session/date/sample dependent."
    if item.param_type in {"pagination", "batch"}:
        return "needs_pagination_or_batch", "Endpoint is reachable but empty; pagination/batch parameters may need expansion."
    return "reachable_empty", "Endpoint is reachable but returned empty data."


def install_endpoint_inventory(
    db_path: str | Path,
    items: list[EndpointInventoryItem],
    *,
    api_base: str = "",
    probe_date: str | None = None,
    run_id: str | None = None,
) -> int:
    con = duckdb.connect(str(db_path))
    try:
        started_at = datetime.now()
        effective_run_id = run_id or f"api_probe_{started_at.strftime('%Y%m%d_%H%M%S_%f')}"
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS api_endpoint_probe_run (
                run_id VARCHAR PRIMARY KEY,
                base_url VARCHAR,
                probe_date DATE,
                endpoint_count INTEGER,
                status VARCHAR,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            INSERT INTO api_endpoint_probe_run(
                run_id, base_url, probe_date, endpoint_count, status,
                started_at, completed_at
            ) VALUES (?, ?, CAST(? AS DATE), ?, 'completed', ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                base_url=excluded.base_url,
                probe_date=excluded.probe_date,
                endpoint_count=excluded.endpoint_count,
                status=excluded.status,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at
            """,
            [effective_run_id, api_base, probe_date, len(items), started_at, datetime.now()],
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS api_endpoint_inventory (
                endpoint VARCHAR PRIMARY KEY,
                category VARCHAR,
                source_kinds VARCHAR,
                source_files VARCHAR,
                param_type VARCHAR,
                param_names VARCHAR,
                probe_params VARCHAR,
                table_name VARCHAR,
                table_rows INTEGER,
                probe_status VARCHAR,
                response_type VARCHAR,
                item_key VARCHAR,
                item_count INTEGER,
                top_level_keys VARCHAR,
                verdict VARCHAR,
                usefulness VARCHAR,
                note VARCHAR,
                probed_at VARCHAR
            )
            """
        )
        con.execute("DELETE FROM api_endpoint_inventory")
        rows = [
            (
                item.endpoint,
                item.category,
                ",".join(item.source_kinds),
                ",".join(item.source_files),
                item.param_type,
                ",".join(item.param_names),
                urllib.parse.urlencode(item.probe_params),
                item.table_name or "",
                item.table_rows,
                item.probe_status,
                item.response_type,
                item.item_key,
                item.item_count,
                ",".join(item.top_level_keys),
                item.verdict,
                item.usefulness,
                item.note,
                item.probed_at,
            )
            for item in items
        ]
        con.executemany(
            """
            INSERT INTO api_endpoint_inventory(
                endpoint, category, source_kinds, source_files, param_type,
                param_names, probe_params, table_name, table_rows, probe_status,
                response_type, item_key, item_count, top_level_keys, verdict,
                usefulness, note, probed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return int(con.execute("SELECT count(*) FROM api_endpoint_inventory").fetchone()[0])
    finally:
        con.close()


def _md_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_No rows._"]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * len(rows[0])) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join((cell or "").replace("|", "/").replace("\n", " ") for cell in row) + " |")
    return lines


def _params_text(item: EndpointInventoryItem) -> str:
    return urllib.parse.urlencode(item.probe_params) if item.probe_params else ",".join(item.param_names)


def render_inventory_report(
    items: list[EndpointInventoryItem],
    *,
    date: str,
    stock_code: str,
    sector_code: str,
) -> str:
    counts = Counter(item.verdict for item in items)
    source_counts = Counter(kind for item in items for kind in item.source_kinds)
    param_counts = Counter(item.param_type for item in items)
    usefulness_counts = Counter(item.usefulness for item in items)
    lines = [
        "# Full API Endpoint Inventory",
        "",
        "## Scope",
        "",
        f"- Date: `{date}`",
        f"- Stock sample: `{stock_code}`",
        f"- Sector sample: `{sector_code}`",
        f"- Discovered endpoints: `{len(items)}`",
        "- Safety: no API key or raw response body is written to this report.",
        "",
        "## Summary",
        "",
    ]
    for name, count in sorted(counts.items()):
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Source Coverage", ""])
    for name, count in sorted(source_counts.items()):
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Parameter Types", ""])
    for name, count in sorted(param_counts.items()):
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Professional Usefulness", ""])
    for name, count in sorted(usefulness_counts.items()):
        lines.append(f"- `{name}`: {count}")

    buckets = [
        ("Stable Available", {"stable_available"}),
        ("Available But Not Stored", {"api_available"}),
        ("Reachable But Empty", {"reachable_empty"}),
        ("Needs Trading Session", {"needs_trading_session"}),
        ("Needs Pagination Or Batch", {"needs_pagination_or_batch"}),
        ("Parameter Uncertain", {"param_uncertain"}),
        ("API Error", {"api_error", "api_not_configured"}),
        ("Low Priority / Reference", set()),
    ]
    for title, verdicts in buckets:
        if title == "Low Priority / Reference":
            group = [item for item in items if item.usefulness in {"low_priority", "reference"}]
        else:
            group = [item for item in items if item.verdict in verdicts]
        lines.extend(["", f"## {title}", ""])
        rows = [["Endpoint", "Params", "Status", "Count", "Rows", "Usefulness", "Sources", "Note"]]
        for item in group:
            rows.append(
                [
                    item.endpoint,
                    _params_text(item),
                    item.probe_status,
                    "" if item.item_count is None else str(item.item_count),
                    "" if item.table_rows is None else str(item.table_rows),
                    item.usefulness,
                    ",".join(item.source_kinds),
                    item.note,
                ]
            )
        lines.extend(_md_table(rows))

    lines.extend(["", "## Full Matrix", ""])
    rows = [["Endpoint", "Category", "Param Type", "Params", "Verdict", "Item Count", "Table", "Rows", "Sources"]]
    for item in items:
        rows.append(
            [
                item.endpoint,
                item.category,
                item.param_type,
                _params_text(item),
                item.verdict,
                "" if item.item_count is None else str(item.item_count),
                item.table_name or "",
                "" if item.table_rows is None else str(item.table_rows),
                ",".join(item.source_kinds),
            ]
        )
    lines.extend(_md_table(rows))
    return "\n".join(lines)
