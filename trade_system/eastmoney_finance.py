#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东方财富 财务 / 资金流 数据中心（datacenter-web.eastmoney.com，免 token，沙箱可用）

为什么重要：
  - 东方财富数据面极全（业绩/资金流/分红…），且本沙箱可达（2026-07-12 实测 HTTP 200）。
  - 这让我们**不依赖 Tushare 中继也能拿财务报表**，多一条独立通路。

参考 akshare 的关键解法（已从源码核对并沙箱实测）：
  1. datacenter-web 用 **columns=ALL** 即可返回该报表全部列，无需逐个枚举列名
     （之前逐列枚举极易踩"报表配置不存在"的坑，现已规避）。
  2. filter 用 (SECURITY_CODE="600519") 形式，单/双引号均可，akshare 用单引号 + = 。
  3. 必须强制 IPv4：curl -4（本沙箱默认走 IPv6 到 eastmoney 会 TLS 握手死循环 → HTTP 000）。
  4. 必须 --compressed（返回 gzip）。
  5. 带浏览器 UA 更稳（实测不加也能通，但保留以防万一）。
  6. 请求层做指数退避重试（参考 akshare request_with_retry：每次新建连接 + 退避 + 抖动）。

已验证可用（2026-07-12，columns=ALL）：
  ✅ RPT_F10_FINANCE_GINCOME（个股业绩报表）—— 全部列：营收/归母/每股收益/同比…
  ✅ RPT_DMSK_TS_STOCKNEW（个股资金流）—— 全部列：超大单/大单/主力 流入流出、涨跌幅…

说明：资产负债 / 现金流 在 datacenter 无对应 reportName（akshare 也不从此取，它用
cninfo/新浪），这两张表请用 Tushare 中继（tushare_relay）补齐。

依赖：仅标准库（subprocess 调 curl，规避 urllib 在沙箱 TLS 不稳 + 可强制 -4）。
"""

import subprocess, json, sys, time, random, urllib.parse

from trade_system.host_limiter import shared_host_limiter
from trade_system.eastmoney_clist_guard import (
    DELAY_CLIST_GUARD,
    DEFAULT_CLIST_GUARD,
    EastmoneyClistUnavailable,
)

_HOST = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# 已验证 reportName（columns=ALL 自动返回全部列，无需列名清单）
_REPORTS = {
    "income":   "RPT_F10_FINANCE_GINCOME",   # 个股业绩报表
    "fundflow": "RPT_DMSK_TS_STOCKNEW",       # 个股资金流
}


def _curl_json(url: str, *, timeout: int = 20):
    """返回解析后的 dict；连接/解析失败返回 None。"""
    out = subprocess.run(
        ["curl", "-s", "-4", "--compressed", "-m", str(timeout),
         "-H", f"User-Agent: {_UA}",
         "-H", "Referer: https://data.eastmoney.com/", url],
        capture_output=True, text=False, timeout=int(timeout) + 10,
    )
    if not out.stdout:
        return None
    # Eastmoney occasionally serves realtime JSON as GBK.  Decode the raw
    # bytes before json parsing so security names are not replacement glyphs.
    for encoding in ("utf-8", "gbk"):
        try:
            text = out.stdout.decode(encoding)
            if "\ufffd" in text:
                continue
            return json.loads(text)
        except Exception:
            continue
    return None


def _query(reportName: str, code: str, pageSize: int = 10,
           sort_col: str = "REPORT_DATE", sort_type: str = "-1"):
    """通用查询（columns=ALL）。返回 list[dict]（全部列）；失败返回 []。
    请求层做指数退避重试（每次新建连接 + 退避 + 抖动），对齐 akshare request_with_retry。"""
    flt = '(SECURITY_CODE="%s")' % code
    url = (f"{_HOST}?reportName={reportName}"
           f"&columns=ALL"
           f"&filter={urllib.parse.quote(flt)}"
           f"&pageSize={pageSize}"
           f"&sortColumns={sort_col}&sortTypes={sort_type}")
    last = None
    for attempt in range(4):
        try:
            d = _curl_json(url)
            if d and d.get("success") and d.get("result"):
                rows = d["result"].get("data") or []
                if rows:
                    # 已按 sortColumns 排好序；兜底再按日期列确认倒序
                    return rows
        except Exception as e:
            last = e
        # 指数退避 + 随机抖动（akshare 风格）
        time.sleep(1.0 * (2 ** attempt) + random.uniform(0.3, 1.0))
    return []


def get_fund_flow_market(trade_date: str | None = None, *, page_size: int = 500,
                         max_pages: int | None = None, pause_seconds: float = 0.35,
                         on_page=None):
    """批量获取全市场个股资金流（东方财富 datacenter）。

    ``RPT_DMSK_TS_STOCKNEW`` supports a market-wide, paginated query.  The
    previous implementation only used the same report with a per-code filter,
    which made intraday coverage depend on the size of the candidate list.
    This function fetches each page once, preserves the raw row, and returns
    normalized fields consumed by ``multi_source_stock_flow``.

    The endpoint is a current snapshot.  If ``trade_date`` is supplied, rows
    from another session are discarded rather than relabelled.
    """
    import datetime as _dt

    target = "".join(ch for ch in str(trade_date or "") if ch.isdigit())[:8]
    page_size = max(1, min(int(page_size), 500))
    base_params = {
        "reportName": _REPORTS["fundflow"],
        "columns": "ALL",
        "pageSize": page_size,
        "pageNumber": 1,
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
    }
    rows: list[dict] = []
    first_result = None
    total_pages = 1
    for page in range(1, (max_pages if max_pages is not None else 10_000) + 1):
        if page > total_pages:
            break
        params = {**base_params, "pageNumber": page}
        if pause_seconds > 0:
            shared_host_limiter.acquire("eastmoney", max(float(pause_seconds), 0.5))
        query = urllib.parse.urlencode(params)
        payload = _curl_json(f"{_HOST}?{query}")
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            if page == 1:
                raise RuntimeError("Eastmoney market fund-flow response has no result payload")
            break
        page_rows = result.get("data") if isinstance(result, dict) else None
        if page == 1:
            first_result = result
            try:
                total_pages = max(1, int(result.get("pages", 1)))
            except (TypeError, ValueError):
                total_pages = 1
        if not isinstance(page_rows, list):
            break
        rows.extend(item for item in page_rows if isinstance(item, dict))
        if on_page is not None:
            on_page(page, page_rows, total_pages, result or {})
        if page < total_pages and pause_seconds > 0:
            time.sleep(float(pause_seconds))
        if max_pages is not None and page >= int(max_pages):
            break

    normalized: list[dict] = []
    for row in rows:
        raw_date = str(row.get("TRADE_DATE") or "")[:10]
        compact = "".join(ch for ch in raw_date if ch.isdigit())[:8]
        if target and compact != target:
            continue
        code = str(row.get("SECURITY_CODE") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        def number(value):
            try:
                return float(value) if value not in (None, "", "-") else None
            except (TypeError, ValueError):
                return None
        super_net = None
        if number(row.get("SUPERDEAL_INFLOW")) is not None and number(row.get("SUPERDEAL_OUTFLOW")) is not None:
            super_net = number(row.get("SUPERDEAL_INFLOW")) - number(row.get("SUPERDEAL_OUTFLOW"))
        large_net = None
        if number(row.get("BIGDEAL_INFLOW")) is not None and number(row.get("BIGDEAL_OUTFLOW")) is not None:
            large_net = number(row.get("BIGDEAL_INFLOW")) - number(row.get("BIGDEAL_OUTFLOW"))
        normalized.append({
            "code": code,
            "date": raw_date[:10],
            "main_net": number(row.get("PRIME_INFLOW")),
            "super_net": super_net,
            "large_net": large_net,
            "mid_net": None,
            "small_net": None,
            "close": number(row.get("CLOSE_PRICE")),
            "change_pct": number(row.get("CHANGE_RATE")),
            "turnover": number(row.get("TURNOVERRATE")),
            "name": row.get("SECURITY_NAME_ABBR") or "",
            "raw": row,
        })
    # A market query can contain duplicate code rows if the source rolls over
    # while pages are being fetched.  Keep the latest row per code.
    deduped = {row["code"]: row for row in normalized}
    return list(deduped.values()), {
        "source": "eastmoney_market",
        "status": "live" if deduped else "failed",
        "trade_date": (_dt.datetime.strptime(target, "%Y%m%d").date().isoformat() if target and len(target) == 8 else None),
        "pages": total_pages,
        "pages_fetched": min(total_pages, len(rows) // page_size + (1 if rows and len(rows) % page_size else 0)),
        "rows": len(deduped),
        "expected_rows": int(first_result.get("count") or 0) if isinstance(first_result, dict) else 0,
    }


def get_fund_flow_market_realtime(trade_date: str, *, page_size: int = 100,
                                  max_pages: int | None = None,
                                  pause_seconds: float = 0.5, on_page=None,
                                  start_page: int = 1, sort_field: str = "f12",
                                  _reconcile: bool = True,
                                  _force_curl: bool = False,
                                  _use_delay: bool = False):
    """Fetch the current-session full-market flow snapshot from ``push2``.

    ``datacenter-web`` is a historical/report endpoint and, during a live
    session, commonly still returns the previous trade date.  The ``clist``
    endpoint is the live route used by Eastmoney's market-flow page.  Its
    transport currently caps responses at about 100 rows even when a larger
    ``pz`` is requested, so callers must page until ``total`` is covered.
    The requested date is attached by the caller only after verifying this is
    today's session; no historical rows are relabelled by this function.
    """
    import datetime as _dt

    target = "".join(ch for ch in str(trade_date or "") if ch.isdigit())[:8]
    if len(target) != 8:
        raise ValueError("trade_date must be YYYY-MM-DD or YYYYMMDD")
    # The live clist route is prone to provider-side TCP resets.  A persisted
    # circuit breaker prevents a failed probe from multiplying into dozens of
    # retries/front-door rotations on every scheduler tick.
    clist_guard = DELAY_CLIST_GUARD if _use_delay else DEFAULT_CLIST_GUARD
    try:
        clist_guard.assert_available()
    except EastmoneyClistUnavailable:
        if not _use_delay:
            return get_fund_flow_market_realtime(
                trade_date,
                page_size=page_size,
                max_pages=max_pages,
                pause_seconds=pause_seconds,
                on_page=on_page,
                start_page=start_page,
                sort_field=sort_field,
                _reconcile=_reconcile,
                _force_curl=_force_curl,
                _use_delay=True,
            )
        raise
    page_size = max(20, min(int(page_size), 500))
    fields = "f2,f3,f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87,f104,f105,f184"
    # Match the stock-fund page's universe filters (exclude funds/ST aliases
    # and retain Shanghai/Shenzhen/Beijing A-share boards).  Beijing is a
    # separate Eastmoney market segment; omitting it made a nominal
    # "full-market" snapshot silently stop at Shanghai/Shenzhen.
    fs = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
          "m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2,"
          "m:0+t:81+s:2048")
    total = 0
    total_pages = 0
    rows: list[dict] = []
    fetched_pages = 0
    start_page = max(1, int(start_page or 1))
    limit = int(max_pages) if max_pages is not None else 10_000
    primary_endpoints = [
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://push2his.eastmoney.com/api/qt/clist/get",
        "https://82.push2.eastmoney.com/api/qt/clist/get",
        # Eastmoney publishes equivalent numeric front doors.  Rotating only
        # after a transport failure avoids hammering one host and is useful
        # when the local proxy has cached a reset on the primary hostname.
        "https://17.push2.eastmoney.com/api/qt/clist/get",
        "https://29.push2.eastmoney.com/api/qt/clist/get",
        "https://79.push2.eastmoney.com/api/qt/clist/get",
        "https://95.push2.eastmoney.com/api/qt/clist/get",
        # During a provider-side TLS/front-door reset, the same public API is
        # still available over HTTP.  It carries no credentials; use it only
        # as a bounded transport fallback and keep the result validation.
        "http://push2.eastmoney.com/api/qt/clist/get",
        "http://push2his.eastmoney.com/api/qt/clist/get",
        "http://82.push2.eastmoney.com/api/qt/clist/get",
        "http://17.push2.eastmoney.com/api/qt/clist/get",
        "http://29.push2.eastmoney.com/api/qt/clist/get",
        "http://79.push2.eastmoney.com/api/qt/clist/get",
        "http://95.push2.eastmoney.com/api/qt/clist/get",
    ]
    delay_endpoints = [
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "http://push2delay.eastmoney.com/api/qt/clist/get",
    ]
    endpoints = delay_endpoints if _use_delay else primary_endpoints
    # Three front doors per probe is enough to distinguish a local/front-door
    # issue from a provider-wide reset.  The old implementation tried all 14
    # URLs (and then eight curl URLs) for every page, which amplified blocks.
    front_door_count = min(2 if _use_delay else 3, len(endpoints))
    guard_state = clist_guard.snapshot()
    last_endpoint = guard_state.get("last_endpoint")
    endpoint_start = endpoints.index(last_endpoint) if last_endpoint in endpoints else 0
    probe_endpoints = [endpoints[(endpoint_start + index) % len(endpoints)] for index in range(front_door_count)]
    try:
        import requests
        http_session = requests.Session()
        # The local proxy intermittently resets Eastmoney's paginated
        # endpoint after a few pages.  Direct requests are stable here;
        # keep the proxy-based curl fallback for restricted environments.
        http_session.trust_env = False
    except Exception:  # pragma: no cover - optional dependency
        http_session = None

    def number(value):
        try:
            return float(value) if value not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None

    for page in range(start_page, limit + 1):
        params = {
            "pn": page, "pz": page_size, "po": 1, "np": 1,
            # f62 is useful for ranking but changes while the market is open.
            # Sorting by the immutable stock code keeps page boundaries stable
            # across a refresh and prevents the same code moving between pages.
            "fltt": 2, "invt": 2, "fid": sort_field, "fs": fs,
            "fields": fields,
            "ut": "8dec03ba335b81bf4ebdf7b29ec27d15" if _use_delay else "b2884a393a59ad64002292a3e90d46a5",
        }
        payload = None
        last_error = None
        last_attempt_endpoint = None
        successful_endpoint = None
        # One primary probe is enough: the independently guarded delay route
        # is the next transport, not fourteen equivalent front-door retries.
        attempt_count = 2 if _use_delay else 1
        for attempt in range(attempt_count):
            try:
                shared_host_limiter.acquire("eastmoney", max(float(pause_seconds), 0.5))
                # The proxy-backed curl path is a separate transport from the
                # direct requests session.  A complete reconciliation pass
                # uses it explicitly after a front-door reset so it does not
                # repeat the same failing transport three more times.
                if _force_curl:
                    curl_endpoint = probe_endpoints[(page - 1 + attempt) % front_door_count]
                    last_attempt_endpoint = curl_endpoint
                    payload = _curl_json(curl_endpoint + "?" + urllib.parse.urlencode(params), timeout=8)
                    curl_data = payload.get("data") if isinstance(payload, dict) else None
                    curl_rows = curl_data.get("diff") if isinstance(curl_data, dict) else None
                    if isinstance(curl_rows, (list, dict)):
                        break
                    last_error = ValueError("curl realtime payload has no data.diff")
                    payload = None
                # requests handles the proxy's intermittent Schannel close
                # more reliably than a bare curl process on Windows.  Keep a
                # curl fallback for installations where requests is absent.
                if payload is not None and _force_curl:
                    # A valid curl response was already accepted above.
                    pass
                else:
                    try:
                        if http_session is None:
                            raise RuntimeError("requests unavailable")
                        # Deliberately omit browser headers: the direct endpoint
                        # is less likely to reset a low-rate machine client when
                        # it uses the minimal AkShare-style request.
                        endpoint = probe_endpoints[(page - 1 + attempt) % front_door_count]
                        last_attempt_endpoint = endpoint
                        verify = not endpoint.split("//", 1)[1].startswith(("17.", "29.", "79.", "95."))
                        if not verify:
                            try:
                                import urllib3
                                urllib3.disable_warnings()
                            except Exception:
                                pass
                        response = http_session.get(endpoint, params=params, timeout=8, verify=verify)
                        response.raise_for_status()
                        try:
                            payload = response.json()
                        except Exception:
                            payload = None
                            for encoding in ("utf-8", "gbk"):
                                try:
                                    text = response.content.decode(encoding)
                                    if "\ufffd" in text:
                                        continue
                                    payload = json.loads(text)
                                    break
                                except Exception:
                                    continue
                            if payload is None:
                                raise ValueError("realtime response encoding/json invalid")
                    except Exception as exc:
                        last_error = exc
                        payload = None
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and isinstance(data.get("diff"), list):
                    successful_endpoint = endpoint if not _force_curl else curl_endpoint
                    break
            except Exception as exc:  # pragma: no cover - network-specific
                last_error = exc
            if attempt < attempt_count - 1:
                time.sleep(min(8.0, 1.0 * (2 ** attempt)) + random.uniform(0.2, 0.8))
        data = payload.get("data") if isinstance(payload, dict) else None
        page_rows = data.get("diff") if isinstance(data, dict) else None
        if isinstance(page_rows, dict):
            page_rows = list(page_rows.values())
        if not isinstance(page_rows, list):
            # Curl's IPv4 transport can still succeed when the direct
            # requests session is reset by a front door.  Reuse the same
            # stable-sort parameters before declaring the page unavailable.
            for curl_endpoint in (probe_endpoints if _use_delay else []):
                last_attempt_endpoint = curl_endpoint
                try:
                    curl_payload = _curl_json(
                        curl_endpoint + "?" + urllib.parse.urlencode(params), timeout=8,
                    )
                    curl_data = curl_payload.get("data") if isinstance(curl_payload, dict) else None
                    curl_rows = curl_data.get("diff") if isinstance(curl_data, dict) else None
                    if isinstance(curl_rows, dict):
                        curl_rows = list(curl_rows.values())
                    if isinstance(curl_rows, list):
                        payload, data, page_rows = curl_payload, curl_data, curl_rows
                        successful_endpoint = curl_endpoint
                        break
                except Exception:
                    continue
        if not isinstance(page_rows, list):
            clist_guard.record_failure(last_error or "no data", endpoint=last_attempt_endpoint)
            if not _use_delay:
                return get_fund_flow_market_realtime(
                    trade_date,
                    page_size=page_size,
                    max_pages=max_pages,
                    pause_seconds=pause_seconds,
                    on_page=on_page,
                    start_page=start_page,
                    sort_field=sort_field,
                    _reconcile=_reconcile,
                    _force_curl=_force_curl,
                    _use_delay=True,
                )
            if page == 1:
                raise RuntimeError(f"Eastmoney realtime clist response invalid: {last_error or 'no data'}")
            raise RuntimeError(f"Eastmoney realtime clist page {page} invalid: {last_error or 'no data'}")
        if not page_rows:
            clist_guard.record_failure("empty clist data.diff", endpoint=successful_endpoint or last_attempt_endpoint)
            if not _use_delay:
                return get_fund_flow_market_realtime(
                    trade_date,
                    page_size=page_size,
                    max_pages=max_pages,
                    pause_seconds=pause_seconds,
                    on_page=on_page,
                    start_page=start_page,
                    sort_field=sort_field,
                    _reconcile=_reconcile,
                    _force_curl=_force_curl,
                    _use_delay=True,
                )
            raise RuntimeError(f"Eastmoney realtime clist page {page} empty")
        if page == start_page:
            # Only a complete first-page transport success clears the breaker;
            # later pages can still fail and will open it again below.
            clist_guard.record_success(successful_endpoint)
        if page == start_page:
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        if not page_rows:
            break
        for item in page_rows:
            if not isinstance(item, dict):
                continue
            code = str(item.get("f12") or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            rows.append({
                "code": code,
                # This route is a live snapshot; the script has already
                # rejected historical dates before persisting it.
                "date": _dt.datetime.strptime(target, "%Y%m%d").date().isoformat(),
                "main_net": number(item.get("f62")),
                "super_net": number(item.get("f66")),
                "large_net": number(item.get("f72")),
                "mid_net": number(item.get("f78")),
                "small_net": number(item.get("f84")),
                "close": number(item.get("f2")),
                "change_pct": number(item.get("f3")),
                "main_ratio": number(item.get("f184")),
                "name": item.get("f14") or "",
                "raw": item,
            })
        fetched_pages = page
        if on_page is not None:
            on_page(page, page_rows, total_pages, {
                "count": total,
                "total": total,
                "source": "eastmoney_intraday_clist_delay" if _use_delay else "eastmoney_intraday_clist",
                "status": "delayed" if _use_delay else "live",
            })
        # Do not stop on a short page: Eastmoney may cap ``pz`` to 100.
        if (total and len({row["code"] for row in rows}) >= total) or page >= total_pages:
            break
        if pause_seconds > 0:
            time.sleep(float(pause_seconds))

    deduped = {row["code"]: row for row in rows}
    meta = {
        "source": "eastmoney_intraday_clist_delay" if _use_delay else "eastmoney_intraday_clist",
        "status": ("delayed" if _use_delay else "live") if deduped else "failed",
        "trade_date": _dt.datetime.strptime(target, "%Y%m%d").date().isoformat(),
        "pages": total_pages or fetched_pages,
        "pages_fetched": fetched_pages,
        "rows": len(deduped),
        "expected_rows": total,
    }
    # A second, full pass is intentionally rare and only runs when the
    # immutable-code pagination still leaves a material gap.  This catches
    # upstream page resets and transient host inconsistencies without turning
    # every normal refresh into double traffic.
    # Do not auto-run a second full pass.  A duplicate-only gap is resumed by
    # the checkpoint collector, which replays all pages when the denominator
    # is larger than the stored unique-row count.  This keeps each scheduler
    # tick bounded and avoids turning a provider reset into a traffic spike.
    meta["reconciliation_passes"] = 1
    return list(deduped.values()), meta


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def get_income_statement(code: str, periods: int = 8):
    """个股业绩报表（全部列）：营业总收入 / 归母净利润 / 每股收益 / 同比 … 按报告期倒序。"""
    return _query(_REPORTS["income"], code, periods, "REPORT_DATE", "-1")


def get_fund_flow(code: str, days: int = 10):
    """个股资金流（全部列）：超大单/大单/主力 流入流出、收盘价、涨跌幅 … 按交易日期倒序。
    关键数值列：SUPERDEAL_INFLOW/OUTFLOW(超大单)、BIGDEAL_INFLOW/OUTFLOW(大单)、
    PRIME_INFLOW(主力净流入估算)、CHANGE_RATE(涨跌幅)。"""
    return _query(_REPORTS["fundflow"], code, days, "TRADE_DATE", "-1")


def query_report(reportName: str, code: str, pageSize: int = 10,
                 sort_col: str = "REPORT_DATE", sort_type: str = "-1"):
    """通用报表查询：传入任意已注册的 reportName + 股票代码，columns=ALL 返回全部列。
    例：query_report("RPT_F10_FINANCE_GINCOME", "600519")"""
    return _query(reportName, code, pageSize, sort_col, sort_type)


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print(f"\n===== {code} 业绩报表（东方财富 datacenter, columns=ALL）=====")
    for r in get_income_statement(code, 3):
        print(f"  {str(r.get('REPORT_DATE',''))[:10]}  营收={r.get('TOTAL_OPERATE_INCOME')}  "
              f"归母净利={r.get('PARENT_NETPROFIT')}  EPS={r.get('BASIC_EPS')}  "
              f"营收同比={r.get('TOTAL_OPERATE_INCOME_YOY')}%")
    print(f"\n===== {code} 个股资金流（数值列已解锁）=====")
    for r in get_fund_flow(code, 2):
        print(f"  {str(r.get('TRADE_DATE',''))[:10]}  收={r.get('CLOSE_PRICE')}  "
              f"涨={r.get('CHANGE_RATE')}%  "
              f"超大单净={r.get('SUPERDEAL_INFLOW') and r.get('SUPERDEAL_OUTFLOW') and r.get('SUPERDEAL_INFLOW')-r.get('SUPERDEAL_OUTFLOW')}  "
              f"大单净={r.get('BIGDEAL_INFLOW') and r.get('BIGDEAL_OUTFLOW') and r.get('BIGDEAL_INFLOW')-r.get('BIGDEAL_OUTFLOW')}")


if __name__ == "__main__":
    main()
