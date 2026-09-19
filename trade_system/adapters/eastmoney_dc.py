"""Eastmoney datacenter-web adapter family (_em_*).

Callers import these provider implementations directly.
"""
from __future__ import annotations

import json
import hashlib
import datetime
import re
import time
import math
import urllib.request as _u
import urllib.parse as _up
import urllib.error as _ue  # noqa: F401

from trade_system.logging_setup import get_logger
from trade_system.http_transport import read_verified_once, request_deadline

try:
    from trade_system.config import SETTINGS as _PROJECT_SETTINGS
except Exception:
    _PROJECT_SETTINGS = {}

logger = get_logger(__name__)

from trade_system.adapters.kline_sources import UA, EM_UT, _auto_decode, _norm_code  # noqa: E402
from trade_system.eastmoney_clist_guard import (  # noqa: E402
    DELAY_CLIST_GUARD,
    DEFAULT_CLIST_GUARD,
    EastmoneyClistUnavailable,
)


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_SESSION_HDR = {"User-Agent": UA,
                  "Referer": "https://quote.eastmoney.com/",
                  "Accept": "application/json, text/plain, */*"}


def _em_get_json(url, params=None, headers=None, timeout=15, post=False, data=None):
    """东财系统一 GET/POST JSON 入口。失败抛异常，交给上层容错/降级。"""
    hdrs = dict(EM_SESSION_HDR)
    if headers:
        hdrs.update(headers)
    if post:
        req = _u.Request(url, data=(_up.urlencode(data).encode() if data else None),
                         headers=hdrs, method="POST")
    else:
        qs = ("?" + _up.urlencode(params)) if params else ""
        req = _u.Request(url + qs, headers=hdrs)
    raw = read_verified_once(req, timeout=timeout, max_bytes=8_000_000)
    return json.loads(_auto_decode(raw))


def _em_get_clist_json(params=None, timeout=15):
    """Low-frequency clist request with an independently guarded delay route."""
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError('clist requires a positive budget of at most 60 seconds')
    deadline = time.monotonic() + timeout
    if request_deadline.get() is not None:
        deadline = min(deadline, request_deadline.get())
    request_params = dict(params or {})
    request_params.setdefault("ut", "8dec03ba335b81bf4ebdf7b29ec27d15")
    routes = (
        ("https://push2.eastmoney.com/api/qt/clist/get", DEFAULT_CLIST_GUARD, "eastmoney"),
        ("https://push2delay.eastmoney.com/api/qt/clist/get", DELAY_CLIST_GUARD, "eastmoney_delay"),
    )
    errors = []
    for endpoint, guard, source in routes:
        if time.monotonic() >= deadline:
            break
        try:
            guard.assert_available()
        except EastmoneyClistUnavailable as exc:
            errors.append(f"{source}: {exc}")
            continue
        try:
            payload = _em_get_json(endpoint, request_params, timeout=deadline-time.monotonic())
            data = payload.get("data") if isinstance(payload, dict) else None
            diff = data.get("diff") if isinstance(data, dict) else None
            if not isinstance(diff, (list, dict)):
                raise ValueError("clist payload has no data.diff")
            guard.record_success(endpoint)
            payload["_clist_source"] = source
            return payload
        except Exception as exc:
            guard.record_failure(exc, endpoint=endpoint)
            errors.append(f"{source}: {exc}")
    raise RuntimeError("; ".join(errors) or "Eastmoney clist unavailable")


def _em_datacenter(report_name, filter_str="", page_size=50,
                   sort_columns="", sort_types="-1", columns="ALL", timeout=15,
                   page_number=1, date_range=None):
    """东财数据中心统一查询（龙虎榜/解禁/融资融券/大宗/股东/分红/研报共用）。
    columns=ALL 解锁全部字段（沙箱实测必须带，否则只回部分列）。"""
    params = {"reportName": report_name, "columns": columns, "filter": filter_str,
              "pageNumber": str(page_number), "pageSize": str(page_size),
              "sortColumns": sort_columns, "sortTypes": sort_types,
              "source": "WEB", "client": "WEB"}
    if date_range is None:
        d = _em_get_json(DATACENTER_URL, params, timeout=timeout)
        return (d.get("result") or {}).get("data") or []
    start, end = date_range
    date_field = sort_columns.split(",")[0]
    start, end = (datetime.date.fromisoformat(v).isoformat() for v in (start, end))
    if start > end or page_number != 1:
        raise ValueError("invalid complete event range")
    params["filter"] += f"({date_field}>='{start}')({date_field}<='{end}')"
    security = re.fullmatch(r'(SECURITY_CODE="([0-9]{6})")', filter_str.strip("()"))
    if not security:
        raise ValueError("event range requires a single security")
    def fetch(page, remaining):
        return _em_get_json(DATACENTER_URL, dict(params, pageNumber=str(page)), timeout=remaining)
    return _complete_event_pages(fetch, security.group(2), page_size, date_field, start, end, report_name, timeout)


def _complete_event_pages(fetch, code, page_size, date_field, start, end, report_name, timeout):
    deadline = min(time.monotonic()+timeout, request_deadline.get() or float('inf'))
    rows, identities, expected = [], set(), None
    for page in range(1, 101):
        if time.monotonic() >= deadline:
            raise TimeoutError("event pagination budget exhausted")
        payload = fetch(page, deadline-time.monotonic())
        result = payload.get("result") or {}
        total, pages, batch = result.get("count"), result.get("pages"), result.get("data")
        if (payload.get("success") is not True or type(total) is not int or total < 0
                or type(pages) is not int or pages != (total+page_size-1)//page_size
                or pages > 100 or not isinstance(batch, list)):
            raise ValueError("event pagination completeness unknown")
        if expected is not None and expected != (total, pages):
            raise ValueError("event pagination changed during collection")
        expected = (total, pages)
        if len(batch) != min(page_size, max(0, total-len(rows))):
            raise ValueError("event page truncated")
        for row in batch:
            day = datetime.date.fromisoformat(str(row.get(date_field, ''))[:10]).isoformat()
            announcement = report_name == "security/ann"
            identity = row.get("art_code") if announcement else json.dumps(row, sort_keys=True, ensure_ascii=False)
            codes = [r.get("stock_code") for r in row.get("codes", [])] if announcement else [row.get("SECURITY_CODE")]
            if code not in codes or not identity or not start <= day <= end or identity in identities:
                raise ValueError("event outside range or duplicate page")
            identities.add(identity)
            rows.append(row)
        if page >= pages:
            return {"rows": rows, "start": start, "end": end, "complete": True,
                    "report": report_name, "date_field": date_field, "total": total}
    raise ValueError("event page budget exhausted")


def _event_result(data, rows):
    raw_rows = data["rows"] if isinstance(data, dict) else data
    rows = [dict(row, id=row.get("id") or hashlib.sha256(json.dumps(raw, sort_keys=True,
        ensure_ascii=False).encode()).hexdigest(), raw=raw) for row, raw in zip(rows, raw_rows)]
    return dict(data, rows=rows) if isinstance(data, dict) else rows or None


def _em_zt_api(endpoint, date, sort, timeout=10):
    """东财涨停板行情中心（push2ex）。返回 data.pool 原始列表。"""
    params = {"ut": EM_UT, "dpt": "wz.ztzt", "Pageindex": 0,
              "pagesize": 10000, "sort": sort, "date": date}
    d = _em_get_json(f"https://push2ex.eastmoney.com/{endpoint}", params, timeout=timeout)
    return (d.get("data") or {}).get("pool") or []


def _fmt_zt_time(t):
    s = str(t).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


# ---------------------------------------------------------------- 龙虎榜（个股 + 全市场）
def _from_em_dragon_tiger(code, date, look_back=30):
    """个股龙虎榜聚合：上榜记录 + 最近上榜买卖席位 TOP5 + 机构净买卖。
    东财 RPT_DAILYBILLBOARD_DETAILSNEW / RPT_BILLBOARD_DAILYDETAILSBUY/SELL。"""
    try:
        from datetime import datetime, timedelta
        sd = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=look_back)).strftime("%Y-%m-%d")
    except Exception:
        sd = "2000-01-01"
    records = _em_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f'(TRADE_DATE>=\'{sd}\')(TRADE_DATE<=\'{date}\')(SECURITY_CODE="{code}")',
        page_size=50, sort_columns="TRADE_DATE", sort_types="-1", timeout=12)
    recs = [{"date": str(r.get("TRADE_DATE", ""))[:10], "reason": r.get("EXPLANATION", ""),
             "net_buy_wan": round((r.get("BILLBOARD_NET_AMT") or 0) / 1e4, 1),
             "turnover": round(float(r.get("TURNOVERRATE") or 0), 2)} for r in records]
    seats = {"buy": [], "sell": []}
    institution = {"buy_amt": 0, "sell_amt": 0, "net_amt": 0}
    if recs:
        latest = recs[0]["date"]
        buy = _em_datacenter("RPT_BILLBOARD_DAILYDETAILSBUY",
                             filter_str=f'(TRADE_DATE=\'{latest}\')(SECURITY_CODE="{code}")',
                             page_size=10, sort_columns="BUY", sort_types="-1", timeout=12)
        sell = _em_datacenter("RPT_BILLBOARD_DAILYDETAILSSELL",
                              filter_str=f'(TRADE_DATE=\'{latest}\')(SECURITY_CODE="{code}")',
                              page_size=10, sort_columns="SELL", sort_types="-1", timeout=12)
        for row in buy[:5]:
            seats["buy"].append({"name": row.get("OPERATEDEPT_NAME", ""),
                                 "buy_wan": round((row.get("BUY") or 0) / 1e4, 1),
                                 "sell_wan": round((row.get("SELL") or 0) / 1e4, 1),
                                 "net_wan": round((row.get("NET") or 0) / 1e4, 1)})
        for row in sell[:5]:
            seats["sell"].append({"name": row.get("OPERATEDEPT_NAME", ""),
                                  "buy_wan": round((row.get("BUY") or 0) / 1e4, 1),
                                  "sell_wan": round((row.get("SELL") or 0) / 1e4, 1),
                                  "net_wan": round((row.get("NET") or 0) / 1e4, 1)})
        for detail, side in ((buy, "buy"), (sell, "sell")):
            for row in detail:
                if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                    amt = (row.get("BUY") or 0) if side == "buy" else (row.get("SELL") or 0)
                    institution["buy_amt" if side == "buy" else "sell_amt"] += amt
        institution["buy_amt"] = round(institution["buy_amt"] / 1e4, 1)
        institution["sell_amt"] = round(institution["sell_amt"] / 1e4, 1)
        institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)
    return {"records": recs, "seats": seats, "institution": institution, "_src": "eastmoney"}


def _from_em_dragon_tiger_daily(date):
    """全市场龙虎榜汇总（某交易日所有上榜股）。"""
    data = _em_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f'(TRADE_DATE>=\'{date}\')(TRADE_DATE<=\'{date}\')',
        page_size=500, sort_columns="BILLBOARD_NET_AMT", sort_types="-1", timeout=15)
    by_code = {}
    for row in data:
        code = str(row.get("SECURITY_CODE", "")).strip()
        if not code:
            continue
        net = (row.get("BILLBOARD_NET_AMT") or 0) / 1e4
        reason = str(row.get("EXPLANATION") or "").strip()
        current = by_code.setdefault(
            code,
            {
                "code": code,
                "name": row.get("SECURITY_NAME_ABBR", ""),
                "reason": reason,
                "close": row.get("CLOSE_PRICE") or 0,
                "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
                "net_buy_wan": 0.0,
                "buy_wan": 0.0,
                "sell_wan": 0.0,
                "turnover": 0.0,
                "net_amount": 0.0,
                "buy_amount": 0.0,
                "sell_amount": 0.0,
                "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2),
            },
        )
        if reason and reason not in str(current.get("reason") or "").split("；"):
            current["reason"] = "；".join(
                part for part in (str(current.get("reason") or ""), reason) if part
            )
        buy_amount = float(row.get("BILLBOARD_BUY_AMT") or 0)
        sell_amount = float(row.get("BILLBOARD_SELL_AMT") or 0)
        net_amount = float(row.get("BILLBOARD_NET_AMT") or 0)
        current["buy_amount"] += buy_amount
        current["sell_amount"] += sell_amount
        current["net_amount"] += net_amount
        current["buy_wan"] += buy_amount / 1e4
        current["sell_wan"] += sell_amount / 1e4
        current["net_buy_wan"] += net
        current["turnover"] += float(row.get("BILLBOARD_DEAL_AMT") or 0)
    stocks = list(by_code.values())
    for item in stocks:
        item["net_buy_wan"] = round(item["net_buy_wan"], 1)
        item["buy_wan"] = round(item["buy_wan"], 1)
        item["sell_wan"] = round(item["sell_wan"], 1)
        item["turnover"] = int(round(item["turnover"]))
        item["net_amount"] = int(round(item["net_amount"]))
        item["buy_amount"] = int(round(item["buy_amount"]))
        item["sell_amount"] = int(round(item["sell_amount"]))
    return {"date": date, "total_records": len(stocks), "stocks": stocks, "_src": "eastmoney"}


# ---------------------------------------------------------------- 融资融券 / 股东户数 / 解禁 / 分红 / 大宗
def _from_em_margin(code, page_size=30):
    """融资融券日级明细。"""
    data = _em_datacenter("RPTA_WEB_RZRQ_GGMX", filter_str=f'(SCODE="{code}")',
                          page_size=page_size, sort_columns="DATE", sort_types="-1", timeout=12)
    out = [{"date": str(r.get("DATE", ""))[:10], "rzye": r.get("RZYE", 0), "rzmre": r.get("RZMRE", 0),
            "rzche": r.get("RZCHE", 0), "rqye": r.get("RQYE", 0), "rqmcl": r.get("RQMCL", 0),
            "rqchl": r.get("RQCHL", 0), "rzrqye": r.get("RZRQYE", 0)} for r in data]
    return out or None


def _from_em_margin_detail_daily(date, page_size=500, max_pages=20):
    """全市场融资融券明细（恢复 xiaodefa margin_detail 的晚到批次）。"""
    rows = []
    for page_number in range(1, max_pages + 1):
        batch = _em_datacenter(
            "RPTA_WEB_RZRQ_GGMX",
            filter_str=f"(DATE='{date}')",
            page_size=page_size,
            sort_columns="SCODE",
            sort_types="1",
            timeout=20,
            page_number=page_number,
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
    return rows


def _from_em_holder(code, page_size=10, start=None, end=None):
    """股东户数变化（季度级）。"""
    data = _em_datacenter("RPT_HOLDERNUM_DET", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="END_DATE", sort_types="-1", timeout=12, date_range=(start, end) if start is not None else None)
    out = [{"date": str(r.get("END_DATE", ""))[:10], "holder_num": r.get("HOLDER_NUM", 0),
            "change_num": r.get("HOLDER_NUM_CHANGE", 0), "change_ratio": r.get("HOLDER_NUM_RATIO", 0),
            "avg_shares": r.get("AVG_HOLD_NUM"), "notice_date": r.get("HOLD_NOTICE_DATE")} for r in (data["rows"] if isinstance(data, dict) else data)]
    return _event_result(data, out)


def _from_em_lockup(code, page_size=15, start=None, end=None):
    """限售解禁历史。"""
    data = _em_datacenter("RPT_LIFT_STAGE", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="FREE_DATE", sort_types="-1", timeout=12, date_range=(start, end) if start is not None else None)
    out = [{"date": str(r.get("FREE_DATE", ""))[:10], "type": r.get("FREE_SHARES_TYPE", ""),
            "shares": r.get("CURRENT_FREE_SHARES"), "able_shares": r.get("ABLE_FREE_SHARES"), "shares_unit": "10000_shares",
            "ratio": r.get("FREE_RATIO", 0)} for r in (data["rows"] if isinstance(data, dict) else data)]
    return _event_result(data, out)


def _from_em_dividend(code, page_size=20, start=None, end=None):
    """分红送转历史。"""
    data = _em_datacenter("RPT_SHAREBONUS_DET", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="EX_DIVIDEND_DATE", sort_types="-1", timeout=12, date_range=(start, end) if start is not None else None)
    out = [{"date": str(r.get("EX_DIVIDEND_DATE", ""))[:10], "bonus_rmb": r.get("PRETAX_BONUS_RMB", 0),
            "transfer_ratio": r.get("IT_RATIO"), "bonus_ratio": r.get("BONUS_RATIO"), "distribution_basis": "per_10_shares",
            "plan": r.get("ASSIGN_PROGRESS", "")} for r in (data["rows"] if isinstance(data, dict) else data)]
    return _event_result(data, out)


def _from_em_block_trade(code, page_size=20, start=None, end=None):
    """大宗交易记录。"""
    data = _em_datacenter("RPT_DATA_BLOCKTRADE", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="TRADE_DATE", sort_types="-1", timeout=12, date_range=(start, end) if start is not None else None)
    out = []
    for r in (data["rows"] if isinstance(data, dict) else data):
        close = r.get("CLOSE_PRICE") or 0
        dp = r.get("DEAL_PRICE") or 0
        prem = ((dp / close - 1) * 100) if close else 0
        out.append({"date": str(r.get("TRADE_DATE", ""))[:10], "price": dp, "close": close,
                    "premium_pct": round(prem, 2), "vol": r.get("DEAL_VOLUME", 0),
                    "amount": r.get("DEAL_AMT", 0), "buyer": r.get("BUYER_NAME", ""),
                    "seller": r.get("SELLER_NAME", "")})
    return _event_result(data, out)


# ---------------------------------------------------------------- 个股资金流 120 日（push2his）
def _from_em_fund_flow_120d(code):
    mkt, pure = _norm_code(code)
    secid = ("1." if mkt == "sh" else "0." if mkt == "sz" else "2.") + pure
    params = {"secid": secid, "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
              "lmt": "120"}
    d = _em_get_json("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                     params, headers={"Referer": "https://quote.eastmoney.com/", "Origin": "https://quote.eastmoney.com"},
                     timeout=12)
    klines = (d.get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        p = line.split(",")
        if len(p) < 7:
            continue
        out.append({"date": p[0],
                    "main_net": float(p[1]) if p[1] != "-" else 0,
                    "small_net": float(p[2]) if p[2] != "-" else 0,
                    "mid_net": float(p[3]) if p[3] != "-" else 0,
                    "large_net": float(p[4]) if p[4] != "-" else 0,
                    "super_net": float(p[5]) if p[5] != "-" else 0})
    return out or None


# ---------------------------------------------------------------- 个股基本面（push2 stock/get）
def _from_em_stock_info(code):
    mkt, pure = _norm_code(code)
    secid = ("1." if mkt == "sh" else "0." if mkt == "sz" else "2.") + pure
    params = {"fltt": "2", "invt": "2", "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
              "secid": secid}
    d = _em_get_json("https://push2.eastmoney.com/api/qt/stock/get", params, timeout=10)
    x = (d or {}).get("data") or {}
    if not x:
        return None
    return {"code": x.get("f57", ""), "name": x.get("f58", ""), "industry": x.get("f127", ""),
            "total_shares": x.get("f84", 0), "float_shares": x.get("f85", 0),
            "mcap": x.get("f116", 0), "float_mcap": x.get("f117", 0),
            "list_date": str(x.get("f189", "")), "price": x.get("f43", 0), "_src": "eastmoney"}


# ---------------------------------------------------------------- 行业板块排名（push2 clist）
def _from_em_industry_rank(top_n=20):
    params = {"pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
              "fid": "f3", "fs": "m:90+t:2",
              "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207"}
    d = _em_get_clist_json(params, timeout=12)
    items = (d.get("data") or {}).get("diff") or []
    if not items:
        return None
    rows = [{"rank": i + 1, "name": it.get("f14", ""), "change_pct": it.get("f3", 0),
             "code": it.get("f12", ""), "up_count": it.get("f104", 0), "down_count": it.get("f105", 0),
             "leader": it.get("f140", ""), "leader_change": it.get("f136", 0)}
            for i, it in enumerate(items)]
    return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows), "_src": "eastmoney"}


def _from_em_sector_flow(top_n=200, *, page=1, page_size=None):
    """行业/概念板块资金流（东方财富 clist，免 token）。

    The source exposes the same normalized f62/f66/f72/f78/f84 fields used by
    the stock money-flow endpoints: main, super-large, large, medium and small
    order net inflow.  Keeping the raw field names in ``raw`` makes calibration
    possible when Eastmoney changes a field while the normalized columns remain
    stable for downstream analysis.
    """
    size = max(1, min(int(page_size or top_n), 5000))
    params = {
        "pn": str(max(1, int(page))), "pz": str(size), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f62", "fs": "m:90+t:2",
              "fields": "f2,f3,f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87,f104,f105,f128,f136,f140,f184",
    }
    d = _em_get_clist_json(params, timeout=15)
    items = (d.get("data") or {}).get("diff") or []
    if isinstance(items, dict):
        items = list(items.values())
    if not items:
        return None

    def n(item, key):
        try:
            value = item.get(key)
            return float(value) if value not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None

    out = []
    for item in items:
        code = item.get("f12")
        if not code:
            continue
        out.append({
            "sector_code": str(code),
            "sector_name": item.get("f14") or "",
            "change_pct": n(item, "f3"),
            "main_net": n(item, "f62"),
            "super_net": n(item, "f66"),
            "large_net": n(item, "f72"),
            "mid_net": n(item, "f78"),
            "small_net": n(item, "f84"),
            "main_ratio": n(item, "f184"),
            "up_count": item.get("f104"),
            "down_count": item.get("f105"),
            "leader": item.get("f140"),
            "leader_change": n(item, "f136"),
            "sector_type": "em_industry",
            "amount_unit": "yuan",
            "_src": d.get("_clist_source") or "eastmoney",
            "raw": item,
        })
    return out or None


def _from_em_sector_flow_page(
    page=1,
    page_size=200,
    *,
    return_meta=False,
    sort_field="f12",
    sort_order="0",
):
    """Fetch one paginated Eastmoney sector-flow page.

    The regular resilient source keeps the historical ``top_n`` API.  The
    intraday full-coverage collector uses this explicit page primitive so it
    can checkpoint page progress without issuing one request per sector.
    """
    params = {
        "pn": str(max(1, int(page))), "pz": str(max(1, int(page_size))),
        # f62 changes during the session, so it is unsafe as a pagination key.
        # f12 is the stable board code; a reverse pass can use the same key
        # without issuing one request per board.
        "po": str(sort_order), "np": "1", "fltt": "2", "invt": "2", "fid": str(sort_field),
        "fs": "m:90+t:2",
        "fields": "f2,f3,f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87,f104,f105,f128,f136,f140,f184",
    }
    d = _em_get_clist_json(params, timeout=15)
    payload = (d.get("data") or {}) if isinstance(d, dict) else {}
    items = payload.get("diff") or []
    if isinstance(items, dict):
        items = list(items.values())
    rows = []
    for item in items:
        if not isinstance(item, dict) or not item.get("f12"):
            continue
        def n(key):
            try:
                value = item.get(key)
                return float(value) if value not in (None, "", "-") else None
            except (TypeError, ValueError):
                return None
        rows.append({
            "sector_code": str(item.get("f12")),
            "sector_name": item.get("f14") or "",
            "change_pct": n("f3"), "main_net": n("f62"),
            "super_net": n("f66"), "large_net": n("f72"),
            "mid_net": n("f78"), "small_net": n("f84"),
            "main_ratio": n("f184"), "up_count": item.get("f104"),
            "down_count": item.get("f105"), "leader": item.get("f140"),
            "leader_change": n("f136"), "sector_type": "em_industry",
            "amount_unit": "yuan", "_src": d.get("_clist_source") or "eastmoney", "raw": item,
        })
    if return_meta:
        total = payload.get("total") or payload.get("count") or 0
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = 0
        return rows, {"total": total, "page": int(page), "page_size": int(page_size),
                      "returned_rows": len(rows), "sort_field": str(sort_field),
                      "sort_order": str(sort_order),
                      "source": d.get("_clist_source") or "eastmoney"}
    return rows


# ---------------------------------------------------------------- 东财涨停/炸板/跌停/昨涨停 四池（push2ex）
def _from_em_zt_pool(date):
    out = []
    for p in _em_zt_api("getTopicZTPool", date, "fbt:asc"):
        out.append({"code": p.get("c"), "name": p.get("n"), "price": (p.get("p") or 0) / 1000,
                    "pct": round(p.get("zdp", 0), 2), "amount": p.get("amount"),
                    "float_cap": p.get("ltsz"), "turnover": round(p.get("hs", 0), 2),
                    "limit_days": p.get("lbc"), "first_seal": _fmt_zt_time(p.get("fbt")),
                    "last_seal": _fmt_zt_time(p.get("lbt")), "seal_fund": p.get("fund"),
                    "break_times": p.get("zbc"), "industry": p.get("hybk", ""),
                    "zt_stat": f'{(p.get("zttj") or {}).get("days", "?")}天{(p.get("zttj") or {}).get("ct", "?")}板'})
    return out or None


def _from_em_zb_pool(date):
    out = []
    for p in _em_zt_api("getTopicZBPool", date, "fbt:asc"):
        out.append({"code": p.get("c"), "name": p.get("n"), "price": (p.get("p") or 0) / 1000,
                    "limit_price": (p.get("ztp") or 0) / 1000, "pct": round(p.get("zdp", 0), 2),
                    "turnover": round(p.get("hs", 0), 2), "first_seal": _fmt_zt_time(p.get("fbt")),
                    "break_times": p.get("zbc"), "amplitude": round(p.get("zf", 0), 2),
                    "speed": round(p.get("zs", 0), 2), "industry": p.get("hybk", ""),
                    "zt_stat": f'{(p.get("zttj") or {}).get("days", "?")}天{(p.get("zttj") or {}).get("ct", "?")}板'})
    return out or None


def _from_em_dt_pool(date):
    out = []
    for p in _em_zt_api("getTopicDTPool", date, "fund:asc"):
        out.append({"code": p.get("c"), "name": p.get("n"), "price": (p.get("p") or 0) / 1000,
                    "pct": round(p.get("zdp", 0), 2), "turnover": round(p.get("hs", 0), 2),
                    "pe": p.get("pe"), "seal_fund": p.get("fund"), "last_seal": _fmt_zt_time(p.get("lbt")),
                    "board_amount": p.get("fba"), "dt_days": p.get("days"), "open_times": p.get("oc"),
                    "industry": p.get("hybk", "")})
    return out or None


def _from_em_yzt_pool(date):
    out = []
    for p in _em_zt_api("getYesterdayZTPool", date, "zs:desc"):
        out.append({"code": p.get("c"), "name": p.get("n"), "price": (p.get("p") or 0) / 1000,
                    "pct": round(p.get("zdp", 0), 2), "turnover": round(p.get("hs", 0), 2),
                    "amplitude": round(p.get("zf", 0), 2), "speed": round(p.get("zs", 0), 2),
                    "y_first_seal": _fmt_zt_time(p.get("yfbt")), "y_limit_days": p.get("ylbc"),
                    "industry": p.get("hybk", ""),
                    "zt_stat": f'{(p.get("zttj") or {}).get("days", "?")}天{(p.get("zttj") or {}).get("ct", "?")}板'})
    return out or None


# ---------------------------------------------------------------- 东财研报（reportapi）
def _from_em_reports(code, max_pages=3):
    out = []
    for page in range(1, max_pages + 1):
        params = {"industryCode": "*", "pageSize": "100", "industry": "*", "rating": "*",
                  "ratingChange": "*", "beginTime": "2000-01-01", "endTime": "2030-01-01",
                  "pageNo": str(page), "fields": "", "qType": "0", "code": code,
                  "rcode": "", "p": str(page), "pageNum": str(page), "pageNumber": str(page)}
        d = _em_get_json("https://reportapi.eastmoney.com/report/list", params,
                         headers={"Referer": "https://data.eastmoney.com/"}, timeout=20)
        rows = d.get("data") or []
        if not rows:
            break
        for r in rows:
            out.append({"date": (r.get("publishDate") or "")[:10], "title": r.get("title", ""),
                        "org": r.get("orgSName", ""), "rating": r.get("emRatingName", ""),
                        "eps_this": r.get("predictThisYearEps"), "eps_next": r.get("predictNextYearEps"),
                        "industry": r.get("indvInduName", ""), "info_code": r.get("infoCode", "")})
        if page >= (d.get("TotalPage", 1) or 1):
            break
    return out or None


# ---------------------------------------------------------------- 东财个股新闻（search-api-web JSONP）
def _from_em_stock_news(code, page_size=20):
    inner = json.dumps({"uid": "", "keyword": code, "type": ["cmsArticleWebOld"], "client": "web",
                        "clientType": "web", "clientVersion": "curr",
                        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                                  "pageIndex": 1, "pageSize": page_size, "preTag": "", "postTag": ""}}},
                       separators=(",", ":"))
    params = {"cb": "jQuery_news", "param": inner}
    try:
        req = _u.Request("https://search-api-web.eastmoney.com/search/jsonp?" + _up.urlencode(params),
                         headers={"User-Agent": UA, "Referer": "https://so.eastmoney.com/"})
        text = read_verified_once(req, timeout=12, max_bytes=8_000_000).decode("utf-8", "ignore")
        json_str = text[text.index("(") + 1: text.rindex(")")]
        d = json.loads(json_str)
    except Exception:
        return None
    articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
    out = [{"title": re.sub(r"<[^>]+>", "", a.get("title", "")),
            "content": re.sub(r"<[^>]+>", "", a.get("content", ""))[:200],
            "time": a.get("date", ""), "source": a.get("mediaName", ""), "url": a.get("url", "")}
           for a in articles]
    return out or None


# ---------------------------------------------------------------- 东财 7x24 快讯（np-weblist）
def _from_em_flash(page_size=50):
    params = {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "",
              "pageSize": str(page_size), "req_trace": str(__import__("uuid").uuid4())}
    d = _em_get_json("https://np-weblist.eastmoney.com/comm/web/getFastNewsList", params,
                     headers={"Referer": "https://kuaixun.eastmoney.com/"}, timeout=10)
    out = [{"title": it.get("title", ""), "summary": (it.get("summary", "") or "")[:200],
            "time": it.get("showTime", "")} for it in (d.get("data") or {}).get("fastNewsList", [])]
    return out or None


# ---------------------------------------------------------------- 财联社电报（cls.cn + 本地签名）
def _from_cls_telegraph(page_size=50):
    import hashlib
    from datetime import datetime
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
              "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
    try:
        req = _u.Request(url, headers={"User-Agent": UA, "Referer": "https://www.cls.cn/"})
        d = json.loads(read_verified_once(req, timeout=10, max_bytes=8_000_000))
    except Exception:
        return None
    out = []
    for it in (d.get("data") or {}).get("roll_data", []) or []:
        ts = it.get("ctime")
        t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        out.append({"title": it.get("title", "") or it.get("brief", ""),
                    "content": it.get("content", "") or it.get("brief", ""), "time": t})
    return out or None


import datetime
from trade_system.adapters._shared import _f
from trade_system.adapters.kline_sources import _read_json, _norm_date


def get_financials(code, periods=8):
    """业绩报表（营收/归母净利/EPS/同比），东方财富 datacenter，沙箱可用。
    返回统一结构 list[dict]：date/revenue/net_profit/eps/revenue_yoy。"""
    try:
        from trade_system.eastmoney_finance import get_income_statement
        rows = get_income_statement(code, periods=periods)
        if not rows:
            return None
        out = []
        for r in rows:
            out.append({
                "date": str(r.get("REPORT_DATE", ""))[:10],
                "revenue": r.get("TOTAL_OPERATE_INCOME"),
                "net_profit": r.get("PARENT_NETPROFIT"),
                "eps": r.get("BASIC_EPS"),
                "revenue_yoy": r.get("TOTAL_OPERATE_INCOME_YOY"),
            })
        return out
    except Exception:
        return None

def get_fund_flow(code, periods=10):
    """个股资金流：东方财富 datacenter；回退仅由来源协调器执行。
    返回统一结构 list[dict]：date/close/pct/super_net/big_net（东财）或 date/close/net_amount/turnover（新浪）。"""
    try:
        from trade_system.eastmoney_finance import get_fund_flow as _ff
        rows = _ff(code, periods)  # eastmoney_finance.get_fund_flow(code, days=...)
    except Exception:
        rows = None
    if rows:
        out = []
        for r in rows:
            sup = (r.get("SUPERDEAL_INFLOW") or 0) - (r.get("SUPERDEAL_OUTFLOW") or 0)
            big = (r.get("BIGDEAL_INFLOW") or 0) - (r.get("BIGDEAL_OUTFLOW") or 0)
            out.append({
                "date": str(r.get("TRADE_DATE", ""))[:10],
                "close": r.get("CLOSE_PRICE"),
                "pct": r.get("CHANGE_RATE"),
                "super_net": sup,
                "big_net": big,
                "_src": "eastmoney"})
        return out
    return None

EM_HOT_BODY = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38"}

def _from_em_hot_rank(top=50):
    try:
        d = _em_get_json("https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
                         post=True, data={**EM_HOT_BODY, "marketType": "", "pageNo": 1, "pageSize": top},
                         headers={"User-Agent": UA}, timeout=10)
        data = d.get("data") or []
        if not data:
            return None
        secids = [("0." if it["sc"].startswith("SZ") else "1.") + it["sc"][2:] for it in data]
        u = _em_get_json("https://push2.eastmoney.com/api/qt/ulist.np/get",
                         params={"ut": "f057cbcbce2a86e2866ab8877db1d059", "fltt": 2, "invt": 2,
                                 "fields": "f14,f3,f12,f2", "secids": ",".join(secids)},
                         headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}, timeout=10)
        diff = (u.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        nm = {x["f12"]: (x.get("f14"), x.get("f2"), x.get("f3")) for x in diff}
    except Exception:
        return None
    out = [{"rank": it["rk"], "code": it["sc"][2:], "name": nm.get(it["sc"][2:], ("", None, None))[0],
            "price": nm.get(it["sc"][2:], ("", None, None))[1],
            "pct": nm.get(it["sc"][2:], ("", None, None))[2],
            "rank_chg": it.get("hisRc")} for it in data]
    return out or None

def _from_em_hot_concept(code):
    prefix = "SH" if code.startswith("6") else "SZ"
    try:
        d = _em_get_json("https://emappdata.eastmoney.com/stockrank/getHotStockRankList",
                         post=True, data={**EM_HOT_BODY, "srcSecurityCode": prefix + code},
                         headers={"User-Agent": UA}, timeout=10)
        data = d.get("data") or []
    except Exception:
        return None
    out = [{"concept": x.get("conceptName"), "bk": x.get("conceptId"), "hit": x.get("hitCount")}
           for x in data]
    return out or None

def _secid(code):
    """东财 secid：沪=1.xxx 深=0.xxx 京=0.xxx。"""
    mkt, pure = _norm_code(code)
    pre = {"sh": "1", "sz": "0", "bj": "0"}.get(mkt, "1")
    return f"{pre}.{pure}"

def _from_em_trends(code, date=None):
    """分时（当日 1 分钟级）：东财 push2his trends2。零依赖 urllib。
    date='YYYYMMDD'，None=今天。返回 {code,name,date,trends:[{time,price,avg,volume,amount,change_pct}]}。"""
    requested_date = _norm_date(
        date or datetime.date.today().strftime("%Y%m%d")
    )
    params = {
        "secid": _secid(code), "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0", "ndays": "1", "forcect": "1",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    d = _em_get_json("https://push2his.eastmoney.com/api/qt/stock/trends2/get",
                     params, timeout=12)
    if not d:
        return None
    data = d.get("data") or {}
    trends = data.get("trends") or []
    if not trends:
        return None
    out = []
    for t in trends:
        p = t.split(",")
        if len(p) < 6:
            continue
        point_date = _norm_date(p[0])
        if len(point_date) != 8 or point_date != requested_date:
            continue
        out.append({
            "time": p[0], "price": float(p[1]), "avg": float(p[2]),
            "volume": float(p[3]), "amount": float(p[4]),
            "change_pct": (float(p[5]) if p[5] not in ("", "-") else None),
        })
    return {"code": _norm_code(code)[1], "name": data.get("name"),
            "date": requested_date, "trends": out} if out else None

def _from_em_all_stocks():
    """全量 A 股列表（东财 push2 clist）：零依赖第二源，异于 tushare_relay。
    返回 [{code,name}]。"""
    params = {"pn": "1", "pz": "5000",
              "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f12,f14"}
    d = _em_get_json("https://push2.eastmoney.com/api/qt/clist/get", params, timeout=20)
    if not d:
        return None
    diff = (d.get("data") or {}).get("diff") or []
    out = [{"code": r.get("f12"), "name": r.get("f14")} for r in diff if r.get("f12")]
    return out or None

def _from_em_northbound():
    """北向资金当日（东财 push2 kamt）：分钟级累计净买入，异于同花顺源。
    返回 {sh_net,sz_net,total_net,update_time}（字段缺失则只含有的）。"""
    params = {"fields1": "f1,f3",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
              "ut": "fa5fd1943c7b386f172d6893dbfba10b"}
    d = _em_get_json("https://push2.eastmoney.com/api/qt/kamt/get", params, timeout=12)
    if not d:
        return None
    data = d.get("data") or {}

    def _num(x):
        try:
            return float(x)
        except Exception:
            return None
    out = {
        "sh_net": _num(data.get("s2n")),     # 沪股通净买入(亿)
        "sz_net": _num(data.get("n2s")),     # 深股通净买入(亿)
        "total_net": _num(data.get("hksh")),  # 北向合计(亿，待实盘校正)
        "update_time": data.get("datetime"),
    }
    return out if any(v is not None for v in out.values()) else None

def _secid_for(code, kind):
    """为指数/ETF/可转债解析东财 secid。code 也可直接传 '1.000001' 形式。"""
    if "." in str(code):
        return str(code)
    code = _norm_code(code)[1]
    if kind == "index":
        pre = "0" if code.startswith("399") else "1"
    elif kind == "etf":
        pre = "0" if code.startswith(("15", "16")) else "1"
    elif kind == "cb":
        pre = "0" if code.startswith("12") else "1"
    else:
        pre = _secid(code).split(".")[0]
    return f"{pre}.{code}"

def _from_em_kline_secid(secid, start, end, fq="qfq", klt=101):
    """通用 K 线（按 secid）：指数/ETF/可转债/股票通用。返回 [{date,open,...,_src}]。"""
    fqt = {"qfq": "1", "hfq": "2", "": "0"}.get(fq, "1")
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt={klt}&fqt={fqt}&beg={_norm_date(start)}&end={_norm_date(end)}&ut={EM_UT}")
    d = _read_json(url, timeout=10)
    rows = ((d or {}).get("data") or {}).get("klines") or []
    out = []
    for line in rows:
        p = line.split(",")
        if len(p) < 7:
            continue
        out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                    "amount": float(p[6]), "pct": (float(p[8]) if len(p) > 8 else None),
                    "volume_unit": "hands", "amount_unit": "yuan",
                    "adjustment": {"0": "none", "1": "qfq", "2": "hfq"}.get(fqt, "unknown"),
                    "_src": "eastmoney"})
    return out or None

def _from_em_quote_secid(secid, fields2="f43,f44,f45,f46,f47,f48,f57,f58,f60,f69,f116,f117,f168,f169,f170"):
    """通用实时快照（按 secid）：指数/ETF/可转债/股票通用。"""
    params = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": fields2, "ut": EM_UT}
    d = _em_get_json("https://push2.eastmoney.com/api/qt/stock/get", params, timeout=10)
    return (d or {}).get("data") or None

def _from_em_index_kline(code, start="20260101", end="20500101", fq="qfq"):
    return _from_em_kline_secid(_secid_for(code, "index"), start, end, fq)

def _from_em_index_spot(code):
    d = _from_em_quote_secid(_secid_for(code, "index"))
    if not d:
        return None
    return {"code": d.get("f57"), "name": d.get("f58"), "price": _f(d, "f43"),
            "pct": _f(d, "f170"), "high": _f(d, "f44"), "low": _f(d, "f45"),
            "open": _f(d, "f46"), "prev_close": _f(d, "f60"),
            "total_mv": _f(d, "f116"), "total_mv_unit": "yuan",
            "time": d.get("f86"), "_src": "eastmoney"}

def _from_em_etf_kline(code, start="20260101", end="20500101", fq="qfq"):
    return _from_em_kline_secid(_secid_for(code, "etf"), start, end, fq)

def _from_em_etf_info(code):
    d = _from_em_quote_secid(_secid_for(code, "etf"))
    if not d:
        return None
    return {"code": d.get("f57"), "name": d.get("f58"), "price": _f(d, "f43"),
            "pct": _f(d, "f170"), "high": _f(d, "f44"), "low": _f(d, "f45"),
            "open": _f(d, "f46"), "prev_close": _f(d, "f60"),
            "total_mv": _f(d, "f116"), "circ_mv": _f(d, "f117"),
            "total_mv_unit": "yuan", "circ_mv_unit": "yuan",
            "time": d.get("f86"), "_src": "eastmoney"}

def _from_em_cb_kline(code, start="20260101", end="20500101", fq="qfq"):
    return _from_em_kline_secid(_secid_for(code, "cb"), start, end, fq)

def _from_em_cb_quote(code):
    d = _from_em_quote_secid(_secid_for(code, "cb"))
    if not d:
        return None
    return {"code": d.get("f57"), "name": d.get("f58"), "price": _f(d, "f43"),
            "pct": _f(d, "f170"), "high": _f(d, "f44"), "low": _f(d, "f45"),
            "open": _f(d, "f46"), "prev_close": _f(d, "f60"),
            "time": d.get("f86"), "_src": "eastmoney"}

def _em_datacenter_try(report_names, filter_str="", page_size=50, timeout=15):
    """尝试多个候选 reportName，返回第一个非空结果（提升实盘命中率）。"""
    for rn in report_names:
        try:
            data = _em_datacenter(rn, filter_str=filter_str, page_size=page_size, timeout=timeout)
            if data:
                return data
        except Exception:
            continue
    return []

def _from_em_disclosure(code, product, page_size=20, start=None, end=None):
    """Explicit products; dates and financial measures retain their provider meaning."""
    report, date_field, fields = {
        "forecast": ("RPT_PUBLIC_OP_NEWPREDICT", "NOTICE_DATE", {
            "report_date":"REPORT_DATE", "type":"PREDICT_TYPE", "content":"PREDICT_CONTENT",
            "metric":"PREDICT_FINANCE", "metric_code":"PREDICT_FINANCE_CODE",
            "forecast_min":"PREDICT_AMT_LOWER", "forecast_max":"PREDICT_AMT_UPPER",
            "change_pct":"ADD_AMP_UPPER"}),
        "express": ("RPT_FCI_PERFORMANCEE", "NOTICE_DATE", {
            "report_date":"REPORT_DATE", "revenue":"TOTAL_OPERATE_INCOME", "revenue_yoy":"YSTZ",
            "net_profit":"PARENT_NETPROFIT", "net_profit_yoy":"JLRTBZCL", "eps":"BASIC_EPS"}),
        "top10_holders": ("RPT_DMSK_HOLDERS", "END_DATE", {
            "end_date":"END_DATE", "notice_date":"NOTICE_DATE", "holder":"HOLDER_NAME",
            "holder_code":"HOLDER_CODE", "rank":"RANK", "ratio":"HOLD_RATIO",
            "shares":"HOLD_NUM", "nature":"HOLDER_NATURE"}),
    }[product]
    secondary = "RANK,HOLDER_CODE" if product == "top10_holders" else "REPORT_DATE,PREDICT_FINANCE_CODE" if product == "forecast" else "REPORT_DATE"
    data = _em_datacenter(report, filter_str=f'(SECURITY_CODE="{_norm_code(code)[1]}")',
        page_size=page_size, sort_columns=date_field+","+secondary, sort_types="-1"+",1"*(secondary.count(",")+1), timeout=12,
        date_range=(start, end) if start is not None else None)
    raw = data["rows"] if isinstance(data, dict) else data
    rows = [dict({name:r.get(field) for name,field in fields.items()}, date=str(r.get(date_field, ""))[:10]) for r in raw]
    for row in rows:
        if product == "forecast":
            row.update(net_profit_min=row["forecast_min"] if row["metric_code"] == "004" else None,
                       net_profit_max=row["forecast_max"] if row["metric_code"] == "004" else None)
    return _event_result(data, rows)


def _from_em_northbound_hist(start="20250101", end="20500101", page_size=500):
    """历史北向资金（沪深港通每日净买入）：东财数据中心。"""
    data = _em_datacenter_try(
        ["RPT_MUTUAL_HISTORY", "RPT_MUTUAL_DETAIL"],
        filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')", page_size=page_size)
    if not data:
        return None
    out = []
    for r in data:
        out.append({"date": r.get("TRADE_DATE") or r.get("DATE"),
                    "sh_net": _f(r, "HUGE_AMOUNT") or _f(r, "SH_NET"),
                    "sz_net": _f(r, "SZ_NET"),
                    "total_net": _f(r, "NET_Buy") or _f(r, "TOTAL_NET")})
    return out or None

def _from_em_bid_ask(code):
    """五档盘口（东财 geo/get）：与腾讯异后端，互为冗余。"""
    secid = _secid(code)
    params = {"secid": secid, "fields1": "f1,f2,f3,f4,f5",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "ut": EM_UT}
    d = _em_get_json("https://push2.eastmoney.com/api/qt/stock/geo/get", params, timeout=10)
    data = (d or {}).get("data") or {}
    if not data:
        return None
    def num(k):
        try:
            return float(data.get(k))
        except Exception:
            return None
    # geo/get 的 f51..f58 为委比/委差/卖五~卖一/买五~买一（版本相关，尽力解析）
    return {"code": data.get("f57"), "name": data.get("f58"), "price": num("f43"),
            "wb": num("f51"), "wc": num("f52"),
            "sell_raw": [num(f"f5{i}") for i in range(3, 8)],
            "buy_raw": [num(f"f5{i}") for i in range(6, 9)], "_src": "eastmoney"}

def _from_em_irm(code):
    """互动易第二源：东财互动（与巨潮 irm 异后端）。"""
    data = _em_datacenter_try(
        ["RPT_WEB_RESPROUND", "RPT_IRM_ASKLIST"],
        filter_str=f'(CODE="{_norm_code(code)[1]}")', page_size=20)
    if not data:
        # 兜底：东财互动 API
        try:
            d = _em_get_json("https://irm.eastmoney.com/api/question/getQuestionList",
                             {"code": _norm_code(code)[1], "pageSize": "20", "pageIndex": "1"},
                             timeout=12)
            rows = (d or {}).get("data") or {}
            data = rows.get("list") or rows.get("questionList") or []
        except Exception:
            data = []
    if not data:
        return None
    out = []
    for r in data[:20]:
        out.append({"question": r.get("question") or r.get("content") or r.get("qContent"),
                    "answer": r.get("answer") or r.get("reply") or r.get("aContent"),
                    "date": r.get("date") or r.get("publishTime") or r.get("postTime")})
    return out or None

def _from_em_announcements(code, page_size=30, start=None, end=None):
    """Official announcement endpoint; preserve article identity and raw security list."""
    code = _norm_code(code)[1]
    params = dict(stock_list=code, ann_type="A", client_source="web", sr="-1",
                  page_size=str(page_size), f_node="0", s_node="0")
    if start is not None:
        params.update(begin_time=start, end_time=end)
    def fetch(page, remaining):
        payload = _em_get_json("https://np-anotice-stock.eastmoney.com/api/security/ann",
            dict(params, page_index=str(page)), timeout=remaining)
        data = payload.get("data") or {}
        total = data.get("total_hits")
        return {"success": payload.get("success") == 1 and not payload.get("error"), "result": {"count": total,
            "pages": (total+page_size-1)//page_size if type(total) is int else None, "data": data.get("list")}}
    data = (_complete_event_pages(fetch, code, page_size, "notice_date", start, end, "security/ann", 12)
            if start is not None else fetch(1, 12)["result"]["data"] or [])
    rows = [dict(id=r.get("art_code"), title=r.get("title"), date=str(r.get("notice_date", ""))[:10],
        type=",".join(c.get("column_name", "") for c in r.get("columns", [])))
        for r in (data["rows"] if isinstance(data, dict) else data)]
    return _event_result(data, rows)

def _from_em_option_tquote(code):
    """期权报价第二源：东财期权（与新浪 sina_option 异后端）。"""
    params = {"secid": _secid(code), "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f168,f69", "ut": EM_UT}
    d = _em_get_json("https://push2.eastmoney.com/api/qt/stock/get", params, timeout=10)
    data = (d or {}).get("data") or {}
    if not data:
        return None
    return {"code": data.get("f57"), "name": data.get("f58"), "price": _f(data, "f43"),
            "pct": _f(data, "f170"), "high": _f(data, "f44"), "low": _f(data, "f45"),
            "open": _f(data, "f46"), "prev_close": _f(data, "f60"), "_src": "eastmoney"}




def _from_em_ipo(start="20260101", end="20500101"):
    """新股 / IPO 日历（东财数据中心）。返回 [{code,name,ipo_date,issue_price,pe,listing_date}]。
    注：reportName 候选为实测常见值，上线前建议用真实样本校准。"""
    data = _em_datacenter_try(
        ["RPT_NEW_STOCK", "RPT_XINGU", "RPT_IPO_CALENDAR"],
        filter_str=f"(IPO_DATE>='{start}')(IPO_DATE<='{end}')", page_size=200, timeout=15)
    if not data:
        return None
    out = []
    for r in data:
        out.append({"code": r.get("SECURITY_CODE") or r.get("CODE") or r.get("SYMBOL"),
                    "name": r.get("SECURITY_NAME_ABBR") or r.get("NAME") or r.get("SECURITY_NAME"),
                    "ipo_date": r.get("IPO_DATE") or r.get("ISSUE_DATE"),
                    "issue_price": _f(r, "ISSUE_PRICE"),
                    "pe": _f(r, "PE_STATIC") or _f(r, "PE") or _f(r, "ISSUE_PE"),
                    "listing_date": r.get("LISTING_DATE") or r.get("ONLINE_DATE")})
    return out or None

def _from_em_macro(indicators=None):
    """宏观经济（东财数据中心）：GDP/CPI/PPI/PMI/M2 等。
    返回 {indicator: [ {date, value, ...} ]}。reportName 候选为实测常见值，建议上线前校准。"""
    CAND = {
        "gdp": ["RPT_ECONOMY_GDP", "EM_GDP_WY", "RPT_ECONOMY_GDP_WY"],
        "cpi": ["RPT_ECONOMY_CPI", "EM_CPI"],
        "ppi": ["RPT_ECONOMY_PPI", "EM_PPI"],
        "pmi": ["RPT_ECONOMY_PMI", "EM_PMI"],
        "m2":  ["RPT_ECONOMY_M2", "EM_M2"],
        "social_financing": ["RPT_ECONOMY_SOCIAL_FINANCING", "EM_SOCIAL_FINANCING"],
    }
    indicators = indicators or list(CAND)
    out = {}
    for k in indicators:
        names = CAND.get(k, [k.upper()])
        data = _em_datacenter_try(names, page_size=60, timeout=15)
        if data:
            rows = []
            for r in data:
                rows.append({kk.lower(): v for kk, v in r.items()})
            out[k] = rows
    return out or None
