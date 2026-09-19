#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Eastmoney report and flow readers using the shared bounded transport.

Pagination preserves provider coverage and delayed-source identity. Acquisition
owns retries; a reader never starts a second private transport or retry loop.
"""

import json, sys, urllib.parse, urllib.request

from trade_system.http_transport import read_verified_once, request_budget, request_deadline

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


def _read_json(url: str, *, timeout: int = 20):
    raw = read_verified_once(urllib.request.Request(url, headers={
        "User-Agent": _UA, "Referer": "https://data.eastmoney.com/"}),
        timeout=timeout, max_bytes=8_000_000)
    # Eastmoney occasionally serves realtime JSON as GBK.  Decode the raw
    # bytes before json parsing so security names are not replacement glyphs.
    for encoding in ("utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
            if "\ufffd" in text:
                continue
            return json.loads(text)
        except Exception:
            continue
    return None


def _query(reportName: str, code: str, pageSize: int = 10,
           sort_col: str = "REPORT_DATE", sort_type: str = "-1"):
    """One report request; failed/empty responses never fabricate rows."""
    flt = '(SECURITY_CODE="%s")' % code
    url = (f"{_HOST}?reportName={reportName}"
           f"&columns=ALL"
           f"&filter={urllib.parse.quote(flt)}"
           f"&pageSize={pageSize}"
           f"&sortColumns={sort_col}&sortTypes={sort_type}")
    payload = _read_json(url)
    if payload and payload.get("success") and payload.get("result"):
        return payload["result"].get("data") or []
    return []


@request_budget(60)
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
            shared_host_limiter.acquire("eastmoney", max(float(pause_seconds), 0.5),
                                        deadline=request_deadline.get())
        query = urllib.parse.urlencode(params)
        payload = _read_json(f"{_HOST}?{query}")
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


@request_budget(60)
def get_fund_flow_market_realtime(trade_date: str, *, page_size: int = 100,
                                  max_pages: int | None = None,
                                  pause_seconds: float = 0.5, on_page=None,
                                  start_page: int = 1, sort_field: str = "f12",
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
    ]
    delay_endpoints = ["https://push2delay.eastmoney.com/api/qt/clist/get"]
    endpoints = delay_endpoints if _use_delay else primary_endpoints
    # Rotate one verified HTTPS front door per page, without private retries.
    front_door_count = min(2 if _use_delay else 3, len(endpoints))
    guard_state = clist_guard.snapshot()
    last_endpoint = guard_state.get("last_endpoint")
    endpoint_start = endpoints.index(last_endpoint) if last_endpoint in endpoints else 0
    probe_endpoints = [endpoints[(endpoint_start + index) % len(endpoints)] for index in range(front_door_count)]
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
        # One selected front door per page. Delayed data is a separate product,
        # with its own qualification; it shares the original call's deadline.
        endpoint = probe_endpoints[(page - 1) % front_door_count]
        last_attempt_endpoint = endpoint
        try:
            shared_host_limiter.acquire("eastmoney", max(float(pause_seconds), 0.5),
                                        deadline=request_deadline.get())
            payload = _read_json(endpoint + "?" + urllib.parse.urlencode(params), timeout=8)
        except TimeoutError:
            raise
        except Exception as exc:
            last_error = exc
        data = payload.get("data") if isinstance(payload, dict) else None
        page_rows = data.get("diff") if isinstance(data, dict) else None
        if isinstance(page_rows, dict):
            page_rows = list(page_rows.values())
        if isinstance(page_rows, list):
            successful_endpoint = endpoint
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
    # Gaps are resumed by the checkpoint collector, never an implicit full retry.
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
