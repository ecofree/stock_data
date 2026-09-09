"""Eastmoney datacenter-web adapter family (_em_*).

Extracted verbatim from ``trade_system.stock_data_sources`` (now a facade).
All names remain importable from the facade for compatibility.
"""
from __future__ import annotations

import json
import re
import urllib.request as _u
import urllib.parse as _up
import urllib.error as _ue  # noqa: F401

from trade_system.logging_setup import get_logger

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
    try:
        with _u.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(_auto_decode(raw))
    except Exception:
        # Eastmoney's push2 front doors intermittently reset urllib/TLS
        # connections during paginated pulls.  Retry GETs through the same
        # public route over a small host/scheme set; POST callers keep the
        # original exception because their payloads are endpoint-specific.
        if post:
            raise
        try:
            import requests
            from urllib.parse import urlsplit, urlunsplit
            parsed = urlsplit(url)
            hosts = [parsed.netloc]
            if "push2" in parsed.netloc:
                hosts.extend([
                    "push2his.eastmoney.com", "82.push2.eastmoney.com",
                    "17.push2.eastmoney.com", "95.push2.eastmoney.com",
                ])
            last_exc = None
            for scheme in (parsed.scheme, "http"):
                for host in dict.fromkeys(hosts):
                    try:
                        target = urlunsplit((scheme, host, parsed.path, "", ""))
                        session = requests.Session()
                        session.trust_env = False
                        response = session.get(
                            target, params=params, headers=hdrs,
                            timeout=timeout, verify=(scheme == "https"),
                        )
                        response.raise_for_status()
                        return response.json()
                    except Exception as exc:
                        last_exc = exc
            if last_exc:
                raise last_exc
        except Exception:
            raise
        raise


def _em_get_clist_json(params=None, timeout=15):
    """Low-frequency clist request with an independently guarded delay route."""
    import requests

    request_params = dict(params or {})
    request_params.setdefault("ut", "8dec03ba335b81bf4ebdf7b29ec27d15")
    routes = (
        ("https://push2.eastmoney.com/api/qt/clist/get", DEFAULT_CLIST_GUARD, "eastmoney"),
        ("https://push2delay.eastmoney.com/api/qt/clist/get", DELAY_CLIST_GUARD, "eastmoney_delay"),
    )
    errors = []
    for endpoint, guard, source in routes:
        try:
            guard.assert_available()
        except EastmoneyClistUnavailable as exc:
            errors.append(f"{source}: {exc}")
            continue
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(
                endpoint, params=request_params, headers=EM_SESSION_HDR,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
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
                   page_number=1):
    """东财数据中心统一查询（龙虎榜/解禁/融资融券/大宗/股东/分红/研报共用）。
    columns=ALL 解锁全部字段（沙箱实测必须带，否则只回部分列）。"""
    params = {"reportName": report_name, "columns": columns, "filter": filter_str,
              "pageNumber": str(page_number), "pageSize": str(page_size),
              "sortColumns": sort_columns, "sortTypes": sort_types,
              "source": "WEB", "client": "WEB"}
    d = _em_get_json(DATACENTER_URL, params, timeout=timeout)
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


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


def _from_em_holder(code, page_size=10):
    """股东户数变化（季度级）。"""
    data = _em_datacenter("RPT_HOLDERNUMLATEST", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="END_DATE", sort_types="-1", timeout=12)
    out = [{"date": str(r.get("END_DATE", ""))[:10], "holder_num": r.get("HOLDER_NUM", 0),
            "change_num": r.get("HOLDER_NUM_CHANGE", 0), "change_ratio": r.get("HOLDER_NUM_RATIO", 0),
            "avg_shares": r.get("AVG_FREE_SHARES", 0)} for r in data]
    return out or None


def _from_em_lockup(code, page_size=15):
    """限售解禁历史。"""
    data = _em_datacenter("RPT_LIFT_STAGE", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="FREE_DATE", sort_types="-1", timeout=12)
    out = [{"date": str(r.get("FREE_DATE", ""))[:10], "type": r.get("FREE_SHARES_TYPE", ""),
            "shares": r.get("FREE_SHARES", 0), "able_shares": r.get("ABLE_FREE_SHARES", 0),
            "ratio": r.get("FREE_RATIO", 0)} for r in data]
    return out or None


def _from_em_dividend(code, page_size=20):
    """分红送转历史。"""
    data = _em_datacenter("RPT_SHAREBONUS_DET", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="EX_DIVIDEND_DATE", sort_types="-1", timeout=12)
    out = [{"date": str(r.get("EX_DIVIDEND_DATE", ""))[:10], "bonus_rmb": r.get("PRETAX_BONUS_RMB", 0),
            "transfer_ratio": r.get("TRANSFER_RATIO", 0), "bonus_ratio": r.get("BONUS_RATIO", 0),
            "plan": r.get("ASSIGN_PROGRESS", "")} for r in data]
    return out or None


def _from_em_block_trade(code, page_size=20):
    """大宗交易记录。"""
    data = _em_datacenter("RPT_DATA_BLOCKTRADE", filter_str=f'(SECURITY_CODE="{code}")',
                          page_size=page_size, sort_columns="TRADE_DATE", sort_types="-1", timeout=12)
    out = []
    for r in data:
        close = r.get("CLOSE_PRICE") or 0
        dp = r.get("DEAL_PRICE") or 0
        prem = ((dp / close - 1) * 100) if close else 0
        out.append({"date": str(r.get("TRADE_DATE", ""))[:10], "price": dp, "close": close,
                    "premium_pct": round(prem, 2), "vol": r.get("DEAL_VOLUME", 0),
                    "amount": r.get("DEAL_AMT", 0), "buyer": r.get("BUYER_NAME", ""),
                    "seller": r.get("SELLER_NAME", "")})
    return out or None


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


def _from_em_limit_up_sentiment(date):
    """打板情绪：涨停/炸板/跌停数 + 炸板率 + 最高连板 + 连板梯队。"""
    zt, zb, dt = _from_em_zt_pool(date), _from_em_zb_pool(date), _from_em_dt_pool(date)
    ladder = {}
    for s in (zt or []):
        ladder[s["limit_days"]] = ladder.get(s["limit_days"], 0) + 1
    zt_n, zb_n = len(zt or []), len(zb or [])
    return {"date": date, "zt_count": zt_n, "zb_count": zb_n, "dt_count": len(dt or []),
            "break_rate": round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else 0,
            "max_height": max((s["limit_days"] for s in (zt or [])), default=0),
            "ladder": dict(sorted(ladder.items())), "_src": "eastmoney"}


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
        text = _u.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
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
        d = json.loads(_u.urlopen(req, timeout=10).read())
    except Exception:
        return None
    out = []
    for it in (d.get("data") or {}).get("roll_data", []) or []:
        ts = it.get("ctime")
        t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        out.append({"title": it.get("title", "") or it.get("brief", ""),
                    "content": it.get("content", "") or it.get("brief", ""), "time": t})
    return out or None
