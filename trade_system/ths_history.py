"""同花顺概念/所属个股快照采集。

同花顺公开的热榜接口返回 ``stock_list[].tag.concept_tag``，可以稳定形成
“概念 -> 热榜个股”关系；当前接口只接受 ``period``，不提供历史日期参数。
因此本模块只把真实抓取日作为 ``trade_date``，所有记录 ``date_verified=False``，
并在结果中显式列出无法补齐的历史日期，避免历史回测误用当前快照。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
import urllib.request
from html import unescape

import duckdb

from base import DuckDBStore
from schema import init_schema
from trade_system.stock_data_sources import _auto_decode, _from_ths_hot_list
from trade_system.host_limiter import shared_host_limiter


DEFAULT_CONCEPT_SOURCE = "ths"
DEFAULT_PERIOD = "day"
THS_CONCEPT_CATALOG_URL = "https://q.10jqka.com.cn/gn/"
# The THS HTML catalog has no historical-date selector.  Only a snapshot
# fetched on the actual current date is verified; historical labels remain
# explicitly unverified so they cannot leak into backtests.
THS_REQUEST_INTERVAL_SECONDS = 0.35
_THS_LAST_REQUEST_AT: float | None = None
# A browser-authenticated session may be supplied explicitly for a repair or
# operator-run crawl.  Scheduled runs do not invent credentials: when this is
# absent, only the public anti-bot cookie is generated and blocked/partial
# pages remain fail-closed.
_THS_COOKIE: str | None = os.getenv("THS_COOKIE", "").strip() or None


def _ths_request_cookie() -> str:
    """Return the short-lived THS anti-bot cookie used by its AJAX pager.

    THS serves the first detail page without a cookie but returns a login/401
    response for the actual ``/ajax/1/`` constituent requests unless the
    JavaScript ``v`` cookie is present.  AkShare already ships the same small
    ``ths.js`` cookie generator; reuse it when available and keep a graceful
    no-cookie fallback for minimal installations/tests.
    """
    global _THS_COOKIE
    if _THS_COOKIE:
        return _THS_COOKIE
    try:
        from py_mini_racer import MiniRacer
        import akshare.stock_feature.stock_board_concept_ths as ak_ths

        context = MiniRacer()
        context.eval(ak_ths._get_file_content_ths("ths.js"))
        value = str(context.call("v") or "").strip()
        if value:
            _THS_COOKIE = f"v={value};"
    except Exception:
        _THS_COOKIE = ""
    return _THS_COOKIE or ""


class _THSCatalog(list):
    """List-compatible catalogue carrying its enumeration provider."""

    def __init__(self, rows: list[tuple[str, str]], provider: str):
        super().__init__(rows)
        self.provider = provider


def _ths_html(url: str, referer: str = THS_CONCEPT_CATALOG_URL) -> str:
    global _THS_LAST_REQUEST_AT
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            if _THS_LAST_REQUEST_AT is not None:
                wait = THS_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _THS_LAST_REQUEST_AT)
                if wait > 0:
                    time.sleep(wait)
            shared_host_limiter.acquire("ths", max(THS_REQUEST_INTERVAL_SECONDS, 0.5))
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                "Referer": referer,
                "Cookie": _ths_request_cookie(),
                "X-Requested-With": "XMLHttpRequest" if "/ajax/1/" in url else "",
                "Accept": "text/html, */*; q=0.01",
            })
            _THS_LAST_REQUEST_AT = time.monotonic()
            with urllib.request.urlopen(request, timeout=20) as response:
                return _auto_decode(response.read())
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown error"
    raise RuntimeError(f"THS page request failed after 3 attempts: {url}; {detail}") from last_error


def _ths_catalog() -> list[tuple[str, str]]:
    """Return the current THS catalogue, preferring the refreshed AKShare adapter."""
    try:
        from trade_system.akshare_guard import ensure_akshare_current

        refresh = ensure_akshare_current()
        import akshare as ak

        frame = ak.stock_board_concept_name_ths()
        columns = {str(column).lower(): column for column in frame.columns}
        code_column = next((columns[key] for key in ("代码", "code", "板块代码") if key in columns), None)
        name_column = next((columns[key] for key in ("名称", "name", "板块名称") if key in columns), None)
        rows = []
        seen = set()
        if code_column is not None and name_column is not None:
            for _, item in frame.iterrows():
                code = re.sub(r"\D", "", str(item.get(code_column) or ""))
                name = str(item.get(name_column) or "").strip()
                if code and name and code not in seen:
                    seen.add(code)
                    rows.append((code, name))
        if len(rows) >= 350:
            return _THSCatalog(rows, f"akshare_ths_{refresh.get('version', '')}")
    except Exception:
        # Direct THS HTML remains the explicit transport fallback.  It is
        # still validated by the full member snapshot before publication.
        pass

    html = _ths_html(THS_CONCEPT_CATALOG_URL)
    items = []
    seen = set()
    for code, raw_name in re.findall(
        r'<a\s+href="(?:https?:)?//q\.10jqka\.com\.cn/gn/detail/code/(\d+)/"[^>]*>([^<]+)</a>',
        html, flags=re.I,
    ):
        name = unescape(re.sub(r"\s+", " ", raw_name)).strip()
        if code and name and code not in seen:
            seen.add(code)
            items.append((code, name))
    return _THSCatalog(items, "ths_html")


def _ths_page_count(html: str) -> int:
    """Read the pagination total rendered by a THS concept detail page."""
    match = re.search(r'class=["\']page_info["\'][^>]*>\s*\d+\s*/\s*(\d+)', html, flags=re.I)
    if not match:
        # Keep this tolerant of minor markup changes (the site has used both
        # quoted and unquoted class attributes over time).
        match = re.search(r'page_info[^>]*>\s*\d+\s*/\s*(\d+)', html, flags=re.I)
    try:
        return max(1, int(match.group(1))) if match else 1
    except (TypeError, ValueError):
        return 1


def _parse_ths_members(html: str) -> list[dict[str, str]]:
    # Detail pages may render a small ``series-table`` before the actual
    # constituent table.  Select the table with the most stock-page links
    # instead of assuming the first <table> is the member grid.
    tables = []
    for match in re.finditer(r"<table\b", html, flags=re.I):
        end = html.find("</table>", match.start())
        if end >= 0:
            table = html[match.start():end]
            tables.append(("m-pager-table" in table.lower(),
                           len(re.findall(r"stockpage\.10jqka\.com\.cn/\d{6}", table, flags=re.I)), table))
    if not tables:
        return []
    # Prefer the paginated constituent grid.  Some boards have a larger
    # ``series-table`` (related sub-series) with more stock links than the
    # actual member page; choosing by link count alone silently truncates the
    # board to one or a few unrelated rows.
    pager_tables = [item for item in tables if item[0]]
    _, _, table = max(pager_tables or tables, key=lambda item: item[1])
    members: list[dict[str, str]] = []
    rows = re.findall(r"<tr>(.*?)</tr>", table, flags=re.I | re.S)
    for row in rows:
        code_match = re.search(r'stockpage\.10jqka\.com\.cn/(\d{6})/?["\']', row, flags=re.I)
        if not code_match:
            continue
        labels = re.findall(
            r'<a[^>]+stockpage\.10jqka\.com\.cn/\d{6}/?[^>]*>([^<]*)</a>', row,
            flags=re.I,
        )
        name = unescape(labels[1] if len(labels) > 1 else (labels[0] if labels else "")).strip()
        members.append({"code": code_match.group(1), "name": name})
    return members


class _THSMembersResult(tuple):
    """Tuple-compatible member result carrying the actual THS provider.

    Keeping this tuple-compatible preserves the small public/test helper API
    while allowing the full collector to audit whether rows came from the
    paginated HTML or the more complete index ``blockrank`` endpoint.
    """

    def __new__(cls, members: list[dict[str, str]], expected_pages: int,
                fetched_pages: int, provider: str = "ths_concept_board",
                advertised_count: int | None = None):
        obj = super().__new__(cls, (members, expected_pages, fetched_pages))
        obj.provider = provider
        obj.advertised_count = advertised_count
        return obj


def _ths_board_index_code(html: str) -> str:
    """Extract the eight-digit THS concept-index code embedded in detail HTML."""
    patterns = (
        r'<input[^>]+id=["\']clid["\'][^>]+value=["\'](\d+)["\']',
        r'<input[^>]+value=["\'](\d+)["\'][^>]+id=["\']clid["\']',
        r'\bclid\b[^0-9]{0,120}(\d{6,})',
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _ths_blockrank_json(index_code: str, amount: int | str) -> dict[str, Any]:
    """Fetch and decode a THS ``blockrank`` JSONP response.

    The endpoint is the same all-members endpoint migrated from AData, but is
    implemented directly so the project does not depend on AData's runtime
    compatibility or its optional JavaScript engine.
    """
    suffix = str(amount) if isinstance(amount, str) else f"d{int(amount)}"
    url = f"https://d.10jqka.com.cn/v2/blockrank/{index_code}/8/{suffix}.js"
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Host": "d.10jqka.com.cn",
        "Referer": "https://q.10jqka.com.cn/gn/",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        text = _auto_decode(response.read())
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"THS blockrank returned invalid JSONP: index={index_code}")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError(f"THS blockrank returned non-object: index={index_code}")
    return payload


def _ths_index_members(index_code: str) -> tuple[list[dict[str, str]], int]:
    """Return all members for a THS index and its advertised member count."""
    probe = _ths_blockrank_json(index_code, 15)
    block = probe.get("block") if isinstance(probe, dict) else {}
    try:
        total = max(0, int(float((block or {}).get("subcodeCount", 0))))
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return [], 0
    # THS accepts a dN request and returns up to N rows.  Round to its normal
    # 15-row page unit; the response itself is authoritative and is validated
    # against subcodeCount below.
    amount = min(3000, max(15, ((total + 14) // 15) * 15))
    payloads = [_ths_blockrank_json(index_code, amount)]
    if total > 3000:
        # AData's migrated implementation uses the documented split endpoints:
        # ``a3000`` for the first batch and ``d3000`` for the remainder.
        payloads = [_ths_blockrank_json(index_code, "a3000"),
                    _ths_blockrank_json(index_code, "d3000")]
    rows: list[dict[str, str]] = []
    response_counts: list[int] = []
    for payload in payloads:
        try:
            response_counts.append(int(float((payload.get("block") or {}).get("subcodeCount", 0))))
        except (TypeError, ValueError):
            pass
        for item in payload.get("items", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            code = _stock_code(item.get("5"))
            if not code or not re.fullmatch(r"\d{6}", code):
                continue
            rows.append({"code": code, "name": str(item.get("55") or "").strip()})
    deduped = {row["code"]: row for row in rows}
    # Constituents can change between the 15-row probe and the full response;
    # trust the full response's advertised count when it is internally
    # consistent, while still rejecting a genuinely truncated payload.
    advertised = response_counts[-1] if response_counts else total
    gap = advertised - len(deduped)
    # THS occasionally leaves a stale ``subcodeCount`` (+1) while the actual
    # blockrank payload is complete.  Treat that bounded mismatch as a valid
    # snapshot and preserve both counts in the collector raw metadata.  Larger
    # gaps remain hard failures; accepting them would publish truncated boards.
    if gap > 1:
        raise RuntimeError(
            f"THS blockrank member coverage incomplete: index={index_code} "
            f"rows={len(deduped)}/{advertised}"
        )
    return list(deduped.values()), advertised


def _ths_detail_members_with_meta(code: str, max_pages: int = 0) -> tuple[list[dict[str, str]], int, int]:
    """Fetch a board's members and return (members, expected_pages, fetched_pages).

    ``max_pages=0`` means discover and fetch every page advertised by THS.
    A positive value is an explicit bounded mode and is reported as partial if
    the board advertises more pages than the bound.
    """
    first_url = f"https://q.10jqka.com.cn/gn/detail/code/{code}/"
    first_html = _ths_html(first_url, THS_CONCEPT_CATALOG_URL)
    expected_pages = _ths_page_count(first_html)
    target_pages = expected_pages if int(max_pages) <= 0 else min(expected_pages, int(max_pages))
    if "m-pager-table" not in first_html.lower():
        raise RuntimeError(f"THS member page blocked or missing table: concept={code} page=1")

    # The HTML endpoint is limited to five pages in the current environment.
    # Detail pages embed an index code (``clid``); use THS's own blockrank API
    # for a complete constituent snapshot before falling back to HTML paging.
    # This is intentionally after the page/table guard so a login/anti-bot
    # response cannot be mistaken for a valid board.
    index_code = _ths_board_index_code(first_html)
    if index_code:
        try:
            index_members, advertised_count = _ths_index_members(index_code)
            if index_members and advertised_count >= len(index_members):
                logical_pages = expected_pages
                return _THSMembersResult(index_members, logical_pages, logical_pages,
                                         "ths_index_blockrank", advertised_count)
        except Exception:
            # Preserve the guarded HTML fallback and its exact partial-page
            # checkpoint if the internal endpoint is unavailable.
            pass
    members = _parse_ths_members(first_html)
    fetched_pages = 1
    for page in range(2, target_pages + 1):
        # The visible page links are JavaScript-only.  The real endpoint is
        # the AJAX route emitted by THS ``mpager``; requesting the pretty URL
        # is what caused the recurring 5-page partial checkpoints.
        url = (
            "https://q.10jqka.com.cn/gn/detail/field/199112/order/desc/"
            f"page/{page}/ajax/1/code/{code}"
        )
        html = _ths_html(url, first_url)
        page_members = _parse_ths_members(html)
        if "m-pager-table" not in html.lower() or not page_members:
            # THS currently redirects blocked pages to a 192-byte upass login
            # script.  Do not count that response as fetched; the collector
            # will persist a partial checkpoint with the exact page gap.
            break
        members.extend(page_members)
        fetched_pages += 1
    deduped: dict[str, dict[str, str]] = {}
    for member in members:
        deduped[member["code"]] = member
    return _THSMembersResult(list(deduped.values()), expected_pages, fetched_pages,
                             "ths_concept_board", len(deduped))


def _ths_detail_members(code: str, max_pages: int = 0) -> list[dict[str, str]]:
    """Compatibility wrapper returning only member rows."""
    return _ths_detail_members_with_meta(code, max_pages)[0]


def fetch_ths_full_membership(max_member_pages: int = 0) -> dict[str, Any]:
    """Fetch THS catalogue plus constituent stocks.

    ``max_member_pages=0`` discovers and fetches every page for every board.
    A positive value is an explicit bounded mode; the caller can inspect the
    returned page metadata before treating the snapshot as complete.
    """
    out: list[dict[str, Any]] = []
    catalog = _ths_catalog()
    failed_codes: list[str] = []
    partial_codes: list[str] = []
    page_meta: dict[str, dict[str, int]] = {}
    for concept_code, concept_name in catalog:
        try:
            members, expected, fetched = _ths_detail_members_with_meta(concept_code, max_member_pages)
            page_meta[concept_code] = {"pages_expected": expected, "pages_fetched": fetched,
                                       "member_rows": len(members)}
            if fetched < expected:
                partial_codes.append(concept_code)
        except Exception:
            failed_codes.append(concept_code)
            members = []
        for rank, member in enumerate(members, 1):
            out.append({
                "rank": rank, "code": member["code"], "name": member["name"],
                "concepts": [{"code": f"THS-{concept_code}", "name": concept_name}],
                "_source": "ths_concept_board",
            })
    return {"rows": out, "catalog": catalog, "failed_codes": failed_codes,
            "partial_codes": partial_codes, "page_meta": page_meta}


def _iso(value: str | date) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) < 8:
        raise ValueError(f"invalid date: {value}")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _weekday_dates(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(_iso(start_date), "%Y-%m-%d").date()
    end = datetime.strptime(_iso(end_date), "%Y-%m-%d").date()
    out: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _concept_code(name: str) -> str:
    digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:12].upper()
    return f"THS-{digest}"


def _concept_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("concept_name") or value.get("label")
    return str(value or "").strip()


def _stock_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if code.startswith(("SH", "SZ", "BJ")):
        code = code[2:]
    # TuShare con_code arrives exchange-suffixed (e.g. 000001.SZ); strip the suffix so
    # every writer stores the bare 6-digit code and web/TuShare merges dedup (P1-1).
    if "." in code:
        code = code.split(".", 1)[0]
    return code


def _ths_name_key(value: Any) -> str:
    """Normalize THS/TuShare concept names without changing the stored label."""
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())
    return text.removesuffix("概念")


def _tushare_ths_provider(catalog: list[tuple[str, str]]):
    """Return (relay client, exact THS catalog map, error) when configured.

    TuShare's ``ths_index``/``ths_member`` is the exact concept-member source.
    The project may still have an old placeholder token in ``.env``; reject
    short values before making 361 doomed requests and let the HTTP/browser
    fallback handle that run.
    """
    try:
        from trade_system.tushare_relay import TushareRelayClient
        # Construct through the shared relay adapter so the project-level
        # .env/config settings are honored as well as process environment
        # variables.  The previous os.environ-only check silently disabled
        # this provider for CLI runs even when the configured relay token was
        # available in the project settings.
        client = TushareRelayClient(timeout=40, retries=2)
        if len(client.token) < 40:
            return None, {}, "missing valid Tushare token"
        rows = client.query_rows("ths_index", {"exchange": "A"},
                                 "ts_code,name,count,exchange,list_date,type")
        by_name = {
            _ths_name_key(row.get("name")): row
            for row in rows if row.get("type") == "N"
        }
        mapping = {
            concept_id: by_name[_ths_name_key(concept_name)]
            for concept_id, concept_name in catalog
            if _ths_name_key(concept_name) in by_name
        }
        return client, mapping, ""
    except Exception as exc:
        return None, {}, f"Tushare THS index unavailable: {type(exc).__name__}: {exc}"[:500]


def _tushare_ths_members(client: Any, ts_code: str) -> list[dict[str, str]]:
    rows = client.query_rows(
        "ths_member", {"ts_code": ts_code},
        "ts_code,con_code,con_name,weight,in_date,out_date,is_new",
    )
    members = []
    for row in rows:
        code = _stock_code(row.get("con_code"))
        if code:
            members.append({"code": code, "name": str(row.get("con_name") or "").strip()})
    deduped = {row["code"]: row for row in members}
    return list(deduped.values())


class THSConceptHistoryCollector:
    """Persist THS concept/member snapshots with board-level checkpoints."""

    def __init__(self, db_path: str | Path, *, fetcher: Callable[[str], Any] | None = None,
                 period: str = DEFAULT_PERIOD, mode: str = "hot", max_member_pages: int = 0,
                 member_source: str = "web", max_concepts: int = 0):
        self.db_path = str(db_path)
        self.store = DuckDBStore(self.db_path)
        init_schema(self.store.conn)
        self.fetcher = fetcher or _from_ths_hot_list
        self.period = period
        self.mode = mode
        # 0 means discover and fetch every page.  Positive values are an
        # explicit bounded/smoke mode and must never be reported complete.
        self.max_member_pages = max(0, int(max_member_pages))
        self.max_concepts = max(0, int(max_concepts))
        if member_source not in {"web", "tushare"}:
            raise ValueError("member_source must be web or tushare")
        self.member_source = member_source
        self.crawler_version = "ths_web_v2"
        self.catalog_hash = ""
        self.started = time.monotonic()

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "THSConceptHistoryCollector":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _checkpoint(self, trade_date: str, status: str, *, rows: int = 0,
                    error: str = "") -> None:
        self.store.conn.execute(
            "INSERT INTO history_fetch_checkpoint(dataset,trade_date,page_no,status,rows_written,attempts,last_error,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(dataset,trade_date,page_no) DO UPDATE SET status=excluded.status, "
            "rows_written=excluded.rows_written,attempts=excluded.attempts,last_error=excluded.last_error,updated_at=excluded.updated_at",
            ["ths_concept_snapshot", _iso(trade_date), 0, status, rows, 1, error[:500], datetime.now()],
        )
        self.store.conn.commit()

    def _done(self, trade_date: str, force: bool) -> bool:
        if force:
            return False
        row = self.store.conn.execute(
            "SELECT status FROM history_fetch_checkpoint WHERE dataset=? AND trade_date=? AND page_no=0",
            ["ths_concept_snapshot", _iso(trade_date)],
        ).fetchone()
        return bool(row and row[0] == "success")

    def _member_checkpoint(self, trade_date: str, concept_code: str, concept_name: str,
                           status: str, *, pages_expected: int = 0, pages_fetched: int = 0,
                           member_rows: int = 0, attempts: int = 0, error: str = "",
                           provider: str | None = None) -> None:
        self.store.conn.execute(
            "INSERT INTO ths_concept_member_checkpoint "
            "(trade_date,concept_code,concept_name,status,pages_expected,pages_fetched,member_rows,attempts,last_error,updated_at,provider,crawler_version,catalog_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,current_timestamp,?,?,?) "
            "ON CONFLICT(trade_date,concept_code) DO UPDATE SET concept_name=excluded.concept_name, "
            "status=excluded.status,pages_expected=excluded.pages_expected,pages_fetched=excluded.pages_fetched, "
            "member_rows=excluded.member_rows,attempts=excluded.attempts,last_error=excluded.last_error,updated_at=excluded.updated_at, "
            "provider=excluded.provider,crawler_version=excluded.crawler_version,catalog_hash=excluded.catalog_hash",
            [_iso(trade_date), concept_code, concept_name, status, int(pages_expected), int(pages_fetched),
            int(member_rows), int(attempts), error[:500], provider or self.member_source,
            self.crawler_version, self.catalog_hash],
        )
        self.store.conn.commit()

    def _cached_complete_members(self, requested_date: str, concept_code: str,
                                 max_age_days: int = 14) -> tuple[list[dict[str, str]], str] | None:
        """Return a complete prior weekly snapshot when live THS is blocked.

        The source date is retained in the caller's raw metadata and the
        resulting checkpoint is ``success_stale``.  That keeps the mapping
        usable for review while preventing the P0 gate from treating it as a
        same-day verified membership fetch.
        """
        try:
            candidate = self.store.conn.execute(
                """
                SELECT h.trade_date, count(*) AS rows, max(c.member_rows) AS expected
                FROM ths_concept_stock_history h
                JOIN ths_concept_member_checkpoint c
                  ON c.trade_date=h.trade_date AND c.concept_code=h.concept_code
                WHERE h.trade_date < CAST(? AS DATE) AND h.concept_code=?
                  AND c.status='success'
                GROUP BY h.trade_date
                HAVING count(*)=max(c.member_rows) AND count(*)>0
                ORDER BY h.trade_date DESC
                LIMIT 1
                """,
                [requested_date, concept_code],
            ).fetchone()
            if not candidate:
                return None
            source_date = str(candidate[0])[:10]
            age = (date.fromisoformat(requested_date) - date.fromisoformat(source_date)).days
            if age < 0 or age > max_age_days:
                return None
            rows = self.store.conn.execute(
                "SELECT stock_code,stock_name FROM ths_concept_stock_history "
                "WHERE trade_date=? AND concept_code=? ORDER BY concept_rank,stock_code",
                [source_date, concept_code],
            ).fetchall()
            members = [{"code": str(row[0]), "name": str(row[1] or "")} for row in rows]
            return (members, source_date) if members else None
        except Exception:
            return None

    def _member_checkpoint_row(self, trade_date: str, concept_code: str):
        return self.store.conn.execute(
            "SELECT status,pages_expected,pages_fetched,member_rows,attempts,last_error,provider,crawler_version,catalog_hash "
            "FROM ths_concept_member_checkpoint WHERE trade_date=? AND concept_code=?",
            [_iso(trade_date), concept_code],
        ).fetchone()

    def _collect_full_snapshot(self, requested_date: str, *, force: bool = False) -> dict[str, Any]:
        """Fetch every THS board independently, persisting a resumable checkpoint.

        A board is only marked ``success`` when every advertised detail page was
        fetched and at least one constituent row was parsed.  This prevents a
        successful HTTP response containing an empty/blocked page from being
        mistaken for a complete concept-to-stock mapping.
        """
        catalog = _ths_catalog()
        self.catalog_hash = hashlib.sha256(
            "\n".join(f"{code}|{name}" for code, name in catalog).encode("utf-8")
        ).hexdigest()
        if not catalog:
            self._checkpoint(requested_date, "empty")
            return {"trade_date": requested_date, "status": "empty", "concept_rows": 0,
                    "member_rows": 0, "date_verified": False, "source": DEFAULT_CONCEPT_SOURCE,
                    "mode": "full", "catalog_count": 0, "failed_concepts": 0,
                    "partial_member_concepts": 0, "missing_member_concepts": 0}

        if force:
            self.store.conn.execute("DELETE FROM ths_concept_daily WHERE trade_date=?", [requested_date])
            self.store.conn.execute("DELETE FROM ths_concept_stock_history WHERE trade_date=?", [requested_date])
            self.store.conn.execute("DELETE FROM ths_concept_member_checkpoint WHERE trade_date=?", [requested_date])
            self.store.conn.commit()

        fetched_date = date.today().isoformat()
        failed_codes: list[str] = []
        partial_codes: list[str] = []
        empty_codes: list[str] = []
        stale_codes: list[str] = []
        # The project default is the actual THS concept webpage.  TuShare is
        # retained as an explicit opt-in provider, never as a silent
        # replacement for the requested web crawl.
        if self.member_source == "tushare":
            tushare_client, tushare_map, tushare_error = _tushare_ths_provider(catalog)
        else:
            tushare_client, tushare_map, tushare_error = None, {}, "web crawler selected"
        supplement_attempted = self.member_source == "tushare"
        verified = self.member_source == "web" and fetched_date == requested_date
        processed_concepts = 0
        for concept_rank, (concept_id, concept_name) in enumerate(catalog, 1):
            concept_code = f"THS-{concept_id}"
            prior = self._member_checkpoint_row(requested_date, concept_code)
            if (
                prior and prior[0] == "success" and not force
                and prior[6] == self.member_source
                and prior[7] == self.crawler_version
                and prior[8] == self.catalog_hash
            ):
                existing = self.store.conn.execute(
                    "SELECT (SELECT stock_count FROM ths_concept_daily WHERE trade_date=? AND concept_code=?), "
                    "(SELECT count(*) FROM ths_concept_stock_history WHERE trade_date=? AND concept_code=?)",
                    [requested_date, concept_code, requested_date, concept_code],
                ).fetchone()
                if (existing and existing[0] is not None and int(existing[0]) > 0
                        and int(existing[0]) == int(existing[1] or 0)
                        and int(prior[3] or 0) == int(existing[1] or 0)
                        and int(prior[1] or 0) == int(prior[2] or 0)):
                    # A resumable retry may encounter rows written by an
                    # earlier version that stored the verified flag only in
                    # raw_json.  Repair the typed flag from the immutable
                    # fetch metadata before skipping the already-complete
                    # board.  This is limited to the actual fetch date and
                    # never backdates historical snapshots.
                    if requested_date == date.today().isoformat():
                        self.store.conn.execute(
                            "UPDATE ths_concept_daily SET date_verified=true "
                            "WHERE trade_date=? AND concept_code=? "
                            "AND json_extract_string(raw_json,'$.fetched_date')=? "
                            "AND json_extract_string(raw_json,'$.date_verified')='true'",
                            [requested_date, concept_code, requested_date],
                        )
                        self.store.conn.execute(
                            "UPDATE ths_concept_stock_history SET date_verified=true "
                            "WHERE trade_date=? AND concept_code=? "
                            "AND json_extract_string(raw_json,'$.fetched_date')=? "
                            "AND json_extract_string(raw_json,'$.date_verified')='true'",
                            [requested_date, concept_code, requested_date],
                        )
                        self.store.conn.commit()
                    continue
            if self.max_concepts and processed_concepts >= self.max_concepts:
                break
            processed_concepts += 1
            attempts = int(prior[4] or 0) + 1 if prior else 1
            self._member_checkpoint(requested_date, concept_code, concept_name, "running", attempts=attempts)
            members: list[dict[str, str]] = []
            expected_pages = fetched_pages = 0
            error = ""
            provider = "ths_concept_board"
            supplement_reason = ""
            source_snapshot_date = ""
            stale_fallback = False
            advertised_member_count: int | None = None
            try:
                ts_row = tushare_map.get(concept_id) if tushare_client else None
                if ts_row:
                    members = _tushare_ths_members(tushare_client, ts_row["ts_code"])
                    # TuShare returns one complete member snapshot, not HTML
                    # pages.  Treat it as one logical page and retain the
                    # provider/count in raw_json for auditability.
                    expected_pages = fetched_pages = 1
                    provider = "tushare_ths_member"
                else:
                    details = _ths_detail_members_with_meta(concept_id, self.max_member_pages)
                    members, expected_pages, fetched_pages = details
                    provider = getattr(details, "provider", provider)
                    advertised_member_count = getattr(details, "advertised_count", None)
            except Exception as exc:
                error = str(exc)
                if not tushare_client and tushare_error:
                    error = f"{tushare_error}; {error}"

            # The public THS detail pages are the default source.  A small
            # number of boards can still be blocked after page 5 or return a
            # one-to-three-row mismatch in the internal blockrank payload.
            # For a production-sized catalogue, lazily use TuShare only for
            # those unresolved boards and merge it as an explicitly recorded
            # supplement; successful web boards never call TuShare.
            if (self.member_source == "web" and len(catalog) > 10
                    and (error or (expected_pages and fetched_pages < expected_pages))):
                if not supplement_attempted:
                    tushare_client, tushare_map, tushare_error = _tushare_ths_provider(catalog)
                    supplement_attempted = True
                ts_row = tushare_map.get(concept_id) if tushare_client else None
                if ts_row:
                    try:
                        supplement = _tushare_ths_members(tushare_client, ts_row["ts_code"])
                        merged = {row["code"]: row for row in members}
                        merged.update({row["code"]: row for row in supplement})
                        if len(merged) > len(members):
                            members = list(merged.values())
                            provider = "ths_web+ths_member_supplement"
                            supplement_reason = f"web_gap={len(members) - len(supplement)}; tushare_rows={len(supplement)}"
                            error = ""
                            fetched_pages = expected_pages or 1
                    except Exception as exc:
                        supplement_reason = f"supplement_error={type(exc).__name__}:{exc}"

            # If the authenticated/live sources are unavailable, keep a
            # recent complete weekly mapping usable for review, but mark it
            # explicitly stale.  It is never counted as a same-day success by
            # the snapshot/P0 gates and its source date is preserved in raw.
            if (not members or error or (expected_pages and fetched_pages < expected_pages)):
                cached = self._cached_complete_members(requested_date, concept_code)
                if cached:
                    members, source_snapshot_date = cached
                    expected_pages = fetched_pages = 1
                    provider = "ths_cached_weekly"
                    stale_fallback = True
                    supplement_reason = (
                        f"cached_source_date={source_snapshot_date}; "
                        f"live_error={error[:180]}"
                    )
                    error = ""

            # Remove stale rows for this board before writing this attempt.
            self.store.conn.execute(
                "DELETE FROM ths_concept_daily WHERE trade_date=? AND concept_code=?",
                [requested_date, concept_code],
            )
            self.store.conn.execute(
                "DELETE FROM ths_concept_stock_history WHERE trade_date=? AND concept_code=?",
                [requested_date, concept_code],
            )
            raw = {"requested_date": requested_date, "fetched_date": fetched_date,
                   "date_verified": verified, "mode": "full", "member_source": self.member_source,
                   "catalog_provider": getattr(catalog, "provider", "unknown"),
                   "ths_concept_id": concept_id,
                   "pages_expected": expected_pages, "pages_fetched": fetched_pages,
                   "provider": provider, "supplement_reason": supplement_reason,
                   "advertised_member_count": advertised_member_count,
                   "stale_fallback": stale_fallback,
                   "source_snapshot_date": source_snapshot_date,
                   "tushare_ths_index_error": tushare_error,
                   "error": error}
            concept_row = (requested_date, concept_code, concept_name, concept_rank, len(members),
                           provider, _json(raw), verified)
            self.store.insert_rows(
                "ths_concept_daily", [concept_row],
                ["trade_date", "concept_code", "concept_name", "rank", "stock_count", "source", "raw_json", "date_verified"],
                replace_on=["trade_date", "concept_code"],
            )
            member_rows = []
            for member_rank, member in enumerate(members, 1):
                member_rows.append((requested_date, concept_code, concept_name, member["code"],
                                    member.get("name", ""), member_rank, provider,
                                    _json({**raw, "member_rank": member_rank}), verified))
            self.store.insert_rows(
                "ths_concept_stock_history", member_rows,
                ["trade_date", "concept_code", "concept_name", "stock_code", "stock_name", "concept_rank", "source", "raw_json", "date_verified"],
                replace_on=["trade_date", "concept_code", "stock_code"],
            )
            self.store.conn.commit()

            if error:
                status = "error"
                failed_codes.append(concept_id)
            elif not members:
                status = "empty"
                empty_codes.append(concept_id)
            elif fetched_pages < expected_pages:
                status = "partial"
                error = f"THS page coverage incomplete: fetched={fetched_pages}/{expected_pages}"
                partial_codes.append(concept_id)
            elif provider == "ths_cached_weekly":
                status = "success_stale"
                stale_codes.append(concept_id)
            else:
                status = "success"
            self._member_checkpoint(requested_date, concept_code, concept_name, status,
                                    pages_expected=expected_pages, pages_fetched=fetched_pages,
                                    member_rows=len(members), attempts=attempts, error=error,
                                    provider=provider)
            if concept_rank == 1 or concept_rank % 10 == 0 or concept_rank == len(catalog):
                print(
                    f"THS web crawl {concept_rank}/{len(catalog)} "
                    f"success={concept_rank - len(failed_codes) - len(partial_codes) - len(empty_codes)} "
                    f"failed={len(failed_codes)} partial={len(partial_codes)} empty={len(empty_codes)}",
                    flush=True,
                )

        concept_count, member_count = self._counts(requested_date).values()
        zero_member = int(self.store.conn.execute(
            "SELECT count(*) FROM ths_concept_daily WHERE trade_date=? AND stock_count=0", [requested_date]
        ).fetchone()[0])
        checkpoint_total, checkpoint_success = self.store.conn.execute(
            "SELECT count(*), sum(CASE WHEN status='success' THEN 1 ELSE 0 END) "
            "FROM ths_concept_member_checkpoint WHERE trade_date=?", [requested_date]
        ).fetchone()
        # Re-read the database rather than trusting in-memory loop counters so
        # an interrupted run can be resumed safely and audited accurately.
        failed_total = int(self.store.conn.execute(
            "SELECT count(*) FROM ths_concept_member_checkpoint WHERE trade_date=? AND status='error'", [requested_date]
        ).fetchone()[0])
        partial_total = int(self.store.conn.execute(
            "SELECT count(*) FROM ths_concept_member_checkpoint WHERE trade_date=? AND status IN ('partial','empty','running')",
            [requested_date],
        ).fetchone()[0])
        complete = (int(concept_count) == len(catalog) and int(checkpoint_success or 0) == len(catalog)
                    and zero_member == 0 and failed_total == 0 and partial_total == 0)
        status = "success" if complete else "partial"
        self._checkpoint(requested_date, status, rows=int(concept_count) + int(member_count),
                         error="; ".join(failed_codes[:5] + partial_codes[:5] + empty_codes[:5]))
        return {"trade_date": requested_date, "status": status, "concept_rows": int(concept_count),
                "member_rows": int(member_count), "input_stocks": int(member_count),
                "date_verified": verified, "source": DEFAULT_CONCEPT_SOURCE, "mode": "full",
                "catalog_count": len(catalog), "failed_concepts": failed_total,
                "partial_member_concepts": partial_total, "missing_member_concepts": zero_member,
                "stale_member_concepts": len(stale_codes),
                "checkpoint_total": int(checkpoint_total or 0),
                "checkpoint_success": int(checkpoint_success or 0)}

    def collect_snapshot(self, trade_date: str | date | None = None, *, force: bool = False,
                         mode: str | None = None) -> dict[str, Any]:
        """Fetch one THS snapshot and persist concept and member rows.

        ``trade_date`` is a storage label only.  It is never marked verified,
        because the THS endpoint does not echo an effective date.
        """
        requested_date = _iso(trade_date or date.today())
        selected_mode = mode or self.mode
        if selected_mode == "full" and not force:
            weekly = self._weekly_snapshot_skip(requested_date)
            if weekly:
                return weekly
        if self._done(requested_date, force):
            counts = self._counts(requested_date)
            verified_concepts = int(self.store.conn.execute(
                "SELECT count(*) FROM ths_concept_daily WHERE trade_date=? AND date_verified",
                [requested_date],
            ).fetchone()[0])
            verified_members = int(self.store.conn.execute(
                "SELECT count(*) FROM ths_concept_stock_history WHERE trade_date=? AND date_verified",
                [requested_date],
            ).fetchone()[0])
            return {"trade_date": requested_date, "status": "skipped", **counts,
                    "date_verified": bool(requested_date == date.today().isoformat()
                                           and verified_concepts == counts["concept_rows"]
                                           and verified_members == counts["member_rows"]),
                    "source": DEFAULT_CONCEPT_SOURCE, "mode": selected_mode,
                    "catalog_count": counts["concept_rows"],
                    "failed_concepts": 0, "partial_member_concepts": 0,
                    "missing_member_concepts": 0}
        self._checkpoint(requested_date, "running")
        if selected_mode == "full":
            try:
                return self._collect_full_snapshot(requested_date, force=force)
            except Exception as exc:
                self._checkpoint(requested_date, "error", error=str(exc))
                return {"trade_date": requested_date, "status": "error", "concept_rows": 0,
                        "member_rows": 0, "date_verified": False, "source": DEFAULT_CONCEPT_SOURCE,
                        "mode": "full", "catalog_count": 0, "failed_concepts": 0,
                        "error": str(exc)[:240]}
        try:
            payload = self.fetcher(self.period)
            catalog = payload.get("catalog", []) if isinstance(payload, dict) else []
            rows = payload.get("rows", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            if not rows and not catalog:
                self._checkpoint(requested_date, "empty")
                return {"trade_date": requested_date, "status": "empty", "concept_rows": 0,
                        "member_rows": 0, "date_verified": False, "source": DEFAULT_CONCEPT_SOURCE,
                        "mode": selected_mode, "catalog_count": len(catalog),
                        "failed_concepts": len(payload.get("failed_codes", [])) if isinstance(payload, dict) else 0}

            # Aggregate labels from the 100-stock hot list.  A concept code is
            # a deterministic local key because this endpoint returns names,
            # not THS board IDs.
            concepts: dict[str, dict[str, Any]] = {}
            for catalog_code, catalog_name in catalog:
                concepts.setdefault(catalog_name, {"members": {}, "min_rank": 999999,
                                                    "raw": [], "codes": {f"THS-{catalog_code}"}})
            for item in rows:
                if not isinstance(item, dict):
                    continue
                stock_code = _stock_code(item.get("code") or item.get("stock_code"))
                stock_name = str(item.get("name") or item.get("stock_name") or "").strip()
                if not stock_code:
                    continue
                try:
                    stock_rank = int(item.get("rank") or item.get("order") or 0)
                except (TypeError, ValueError):
                    stock_rank = 0
                labels = item.get("concepts") or (item.get("tag") or {}).get("concept_tag") or []
                if isinstance(labels, str):
                    labels = [labels]
                for raw_label in labels:
                    name = _concept_name(raw_label)
                    if not name:
                        continue
                    entry = concepts.setdefault(name, {"members": {}, "min_rank": stock_rank or 999999,
                                                       "raw": [], "codes": set()})
                    if isinstance(raw_label, dict):
                        label_code = str(raw_label.get("code") or raw_label.get("concept_code") or "").strip()
                        if label_code:
                            entry["codes"].add(label_code)
                    entry["min_rank"] = min(entry["min_rank"], stock_rank or 999999)
                    entry["members"][stock_code] = {
                        "stock_name": stock_name,
                        "hot_rank": stock_rank,
                        "raw": item,
                    }
                    entry["raw"].append(raw_label)

            ordered = sorted(concepts.items(), key=lambda pair: (pair[1]["min_rank"], pair[0]))
            concept_rows = []
            member_rows = []
            fetched_date = date.today().isoformat()
            for rank, (name, entry) in enumerate(ordered, 1):
                code = sorted(entry["codes"])[0] if entry["codes"] else _concept_code(name)
                raw = {"requested_date": requested_date, "fetched_date": fetched_date,
                        "date_verified": False, "period": self.period, "mode": selected_mode,
                       "members": list(entry["members"].values())}
                concept_rows.append((requested_date, code, name, rank, len(entry["members"]),
                                     "ths_concept_board" if selected_mode == "full" else "ths_hot_list", _json(raw), False))
                for stock_code, member in entry["members"].items():
                    member_rows.append((requested_date, code, name, stock_code,
                                         member["stock_name"], rank,
                                         "ths_concept_board" if selected_mode == "full" else "ths_hot_list",
                                        _json({"requested_date": requested_date, "fetched_date": fetched_date,
                                               "date_verified": False, "hot_rank": member["hot_rank"],
                                               "payload": member["raw"]}), False))

            self.store.conn.execute("DELETE FROM ths_concept_daily WHERE trade_date=?", [requested_date])
            self.store.conn.execute("DELETE FROM ths_concept_stock_history WHERE trade_date=?", [requested_date])
            concept_count = self.store.insert_rows(
                "ths_concept_daily", concept_rows,
                ["trade_date", "concept_code", "concept_name", "rank", "stock_count", "source", "raw_json", "date_verified"],
                replace_on=["trade_date", "concept_code"],
            )
            member_count = self.store.insert_rows(
                "ths_concept_stock_history", member_rows,
                ["trade_date", "concept_code", "concept_name", "stock_code", "stock_name", "concept_rank", "source", "raw_json", "date_verified"],
                replace_on=["trade_date", "concept_code", "stock_code"],
            )
            self.store.conn.commit()
            self._checkpoint(requested_date, "success", rows=concept_count + member_count)
            return {"trade_date": requested_date, "status": "success", "concept_rows": concept_count,
                    "member_rows": member_count, "input_stocks": len(rows), "date_verified": False,
                    "source": DEFAULT_CONCEPT_SOURCE, "mode": selected_mode,
                    "catalog_count": len(catalog),
                    "failed_concepts": len(payload.get("failed_codes", [])) if isinstance(payload, dict) else 0}
        except Exception as exc:
            self._checkpoint(requested_date, "error", error=str(exc))
            return {"trade_date": requested_date, "status": "error", "concept_rows": 0,
                    "member_rows": 0, "date_verified": False, "source": DEFAULT_CONCEPT_SOURCE,
                    "mode": selected_mode,
                    "catalog_count": len(catalog),
                    "failed_concepts": len(payload.get("failed_codes", [])) if isinstance(payload, dict) else 0,
                    "error": str(exc)[:240]}

    def run(self, start_date: str, end_date: str, *, force: bool = False) -> dict[str, Any]:
        """Run one current snapshot and report all unavailable historical dates."""
        requested_dates = _weekday_dates(start_date, end_date)
        today = date.today().isoformat()
        snapshot_date = today if today in requested_dates else today
        snapshot = self.collect_snapshot(snapshot_date, force=force)
        missing_dates = [item for item in requested_dates if item != snapshot_date]
        return {
            "start_date": _iso(start_date), "end_date": _iso(end_date),
            "requested_dates": requested_dates, "snapshot": snapshot,
            "missing_historical_dates": missing_dates,
            "historical_supported": False,
            "source": DEFAULT_CONCEPT_SOURCE,
        }

    def _counts(self, trade_date: str) -> dict[str, int]:
        concept_count = self.store.conn.execute(
            "SELECT count(*) FROM ths_concept_daily WHERE trade_date=?", [trade_date]
        ).fetchone()[0]
        member_count = self.store.conn.execute(
            "SELECT count(*) FROM ths_concept_stock_history WHERE trade_date=?", [trade_date]
        ).fetchone()[0]
        return {"concept_rows": int(concept_count), "member_rows": int(member_count)}

    def _weekly_snapshot_skip(self, requested_date: str) -> dict[str, Any] | None:
        """Skip a new daily crawl when a complete THS snapshot exists this week.

        A partially started requested date is deliberately resumed instead of
        being hidden by an older weekly snapshot.  This keeps the full catalogue
        crawl resumable while preventing normal intraday/after-close loops
        from hitting THS every day.
        """
        try:
            target = date.fromisoformat(str(requested_date)[:10])
            catalog_target = len(_ths_catalog())
            if catalog_target < 350:
                return None
            current_rows = int(self.store.conn.execute(
                "SELECT count(*) FROM ths_concept_member_checkpoint WHERE trade_date=?",
                [requested_date],
            ).fetchone()[0] or 0)
            rows = self.store.conn.execute(
                """SELECT trade_date,count(*) AS total,
                          sum(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok
                     FROM ths_concept_member_checkpoint
                    GROUP BY trade_date ORDER BY trade_date DESC"""
            ).fetchall()
            for raw_date, total, ok in rows:
                if raw_date is None:
                    continue
                snapshot_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date)[:10])
                if snapshot_date.isocalendar()[:2] != target.isocalendar()[:2]:
                    continue
                if int(total or 0) != catalog_target or int(ok or 0) != int(total or 0):
                    continue
                if current_rows:
                    # The requested date may be an interrupted resume; let
                    # _collect_full_snapshot continue from its checkpoints.
                    if snapshot_date == target:
                        counts = self._counts(requested_date)
                        return {"trade_date": requested_date, "status": "skipped", **counts,
                                "date_verified": False, "source": DEFAULT_CONCEPT_SOURCE,
                                "mode": "full", "catalog_count": int(total or 0),
                                "weekly_source_date": str(snapshot_date),
                                "failed_concepts": 0, "partial_member_concepts": 0,
                                "missing_member_concepts": 0}
                    continue
                counts = self._counts(str(snapshot_date))
                return {"trade_date": requested_date, "status": "skipped", **counts,
                        "date_verified": False, "source": DEFAULT_CONCEPT_SOURCE,
                        "mode": "full", "catalog_count": int(total or 0),
                        "weekly_source_date": str(snapshot_date),
                        "failed_concepts": 0, "partial_member_concepts": 0,
                        "missing_member_concepts": 0}
        except Exception:
            return None
        return None


def render_report(db_path: str | Path, result: dict[str, Any], out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        concept_count, member_count, verified_concepts, verified_members, latest, zero_member_concepts = con.execute(
            "SELECT (SELECT count(*) FROM ths_concept_daily), (SELECT count(*) FROM ths_concept_stock_history), "
            "(SELECT count(*) FROM ths_concept_daily WHERE date_verified), "
            "(SELECT count(*) FROM ths_concept_stock_history WHERE date_verified), "
            "(SELECT max(trade_date) FROM ths_concept_daily), "
            "(SELECT count(*) FROM ths_concept_daily WHERE trade_date=(SELECT max(trade_date) FROM ths_concept_daily) AND stock_count=0)"
        ).fetchone()
        checkpoint_total, checkpoint_success, checkpoint_partial, checkpoint_stale, checkpoint_errors, checkpoint_empty = con.execute(
            "SELECT count(*), sum(CASE WHEN status='success' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN status='partial' THEN 1 ELSE 0 END), sum(CASE WHEN status='success_stale' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN status='error' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN status='empty' THEN 1 ELSE 0 END) "
            "FROM ths_concept_member_checkpoint WHERE trade_date=(SELECT max(trade_date) FROM ths_concept_member_checkpoint)"
        ).fetchone()
        partial_rows = con.execute(
            "SELECT c.concept_code,c.concept_name,k.status,k.pages_expected,k.pages_fetched,k.member_rows "
            "FROM ths_concept_daily c JOIN ths_concept_member_checkpoint k "
            "USING(trade_date,concept_code) WHERE c.trade_date=(SELECT max(trade_date) FROM ths_concept_daily) "
            "AND k.status<>'success' ORDER BY c.concept_code"
        ).fetchall()
        source_counts = con.execute(
            "SELECT source,count(*) FROM ths_concept_stock_history "
            "WHERE trade_date=(SELECT max(trade_date) FROM ths_concept_stock_history) "
            "GROUP BY source ORDER BY source"
        ).fetchall()
    finally:
        con.close()
    missing = result.get("missing_historical_dates") or []
    lines = [
        "# 同花顺概念与所属个股采集报告", "",
        f"- source: `{DEFAULT_CONCEPT_SOURCE}` (THS concept catalogue)",
        f"- requested_range: `{result.get('start_date')}` ~ `{result.get('end_date')}`",
        f"- snapshot_date: `{result.get('snapshot', {}).get('trade_date') or '-'}`",
        f"- mode: `{result.get('snapshot', {}).get('mode') or '-'}`",
        f"- catalog_count: `{result.get('snapshot', {}).get('catalog_count', 0)}`",
        f"- failed_concepts: `{result.get('snapshot', {}).get('failed_concepts', 0)}`",
        f"- zero_member_concepts: `{zero_member_concepts}`",
        f"- member_checkpoints: `{int(checkpoint_success or 0)}/{int(checkpoint_total or 0)} success`",
        f"- partial_member_concepts: `{result.get('snapshot', {}).get('partial_member_concepts', int(checkpoint_partial or 0))}`",
        f"- stale_member_concepts: `{result.get('snapshot', {}).get('stale_member_concepts', int(checkpoint_stale or 0))}` (usable cache, not same-day success)",
        f"- empty_member_concepts: `{int(checkpoint_empty or 0)}`",
        f"- member_checkpoint_errors: `{int(checkpoint_errors or 0)}`",
        "- member_rows_by_provider: " + ", ".join(f"`{source}`={count}" for source, count in source_counts),
        f"- historical_supported: `{result.get('historical_supported')}`",
        f"- missing_historical_dates: `{len(missing)}`",
        "", "| table | rows | latest_date | verified_rows |", "|---|---:|---|---:|",
        f"| ths_concept_daily | {concept_count} | {latest or '-'} | {verified_concepts} |",
        f"| ths_concept_stock_history | {member_count} | {latest or '-'} | {verified_members} |",
        "", "## 已记录问题", "",
        f"- 同花顺概念板块页面当前可解析完整概念目录（本次 {result.get('snapshot', {}).get('catalog_count', 0)} 个）；已优先使用网页/内部 blockrank 成分接口，失败板块才记录为部分或错误。",
        f"- THS 全量概念及成分股按自然周更新（本次目录 {result.get('snapshot', {}).get('catalog_count', 0)} 个）；日常盘中/盘后任务只读取最近一次完整快照，不重复抓取。",
        "- 同花顺概念板块页面没有历史日期参数，只能保存抓取日快照；`date_verified=0` 是预期保护，不是已补齐的 2026 历史。",
    ]
    lines.append("- Completeness rule: max_member_pages=0 discovers all THS pages; any failed, empty, or bounded board remains partial.")
    lines.append("- THS anti-bot guard: a page without the paginated member table (including upass redirect) is not counted as fetched; rerun/resume is required before historical use.")
    if partial_rows:
        lines.extend(["", "## Partial concept boards", "", "| concept_code | concept_name | status | pages expected | pages fetched | member rows |", "|---|---|---|---:|---:|---:|"])
        lines.extend(f"| {code} | {name} | {status} | {expected} | {fetched} | {members} |"
                     for code, name, status, expected, fetched, members in partial_rows)
    if missing:
        lines.append(f"- 2026 历史概念日期缺失 {len(missing)} 个（首个 `{missing[0]}`，末个 `{missing[-1]}`）；不得用于历史回测。")
    if result.get("snapshot", {}).get("status") == "empty":
        lines.append("- 本次 THS 热榜返回空，概念与所属个股本次未更新。")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
