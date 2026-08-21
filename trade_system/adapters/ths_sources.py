"""THS (10jqka) source adapters.

Extracted verbatim from ``trade_system.stock_data_sources`` (now a facade).
All names remain importable from the facade for compatibility.
"""
from __future__ import annotations

import json
import re
import urllib.error as _ue  # noqa: F401

from trade_system.logging_setup import get_logger

try:
    from trade_system.config import SETTINGS as _PROJECT_SETTINGS
except Exception:
    _PROJECT_SETTINGS = {}

logger = get_logger(__name__)

from trade_system.adapters._shared import _d, _f  # noqa: E402
from trade_system.adapters.eastmoney_dc import _em_get_json  # noqa: E402
from trade_system.adapters.kline_sources import UA, _auto_decode, _norm_code


def _from_ths_northbound():
    """同花顺沪/深股通分钟字段（单位亿元，语义需以当日上游说明为准）。
    源 data.hexin.cn，免 token，沙箱出口被限（本机可用）。
    返回 dict：time 时间点列表 + hgt/sgt 列表 + 末值汇总（latest_hgt/sgt/total）。"""
    import urllib.request as _u
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    req = _u.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
        "Host": "data.hexin.cn", "Referer": "https://data.hexin.cn/"})
    try:
        raw = _u.urlopen(req, timeout=12).read()
        d = json.loads(_auto_decode(raw))
    except Exception:
        return None
    times = d.get("time") or []
    hgt = d.get("hgt") or []
    sgt = d.get("sgt") or []
    # The endpoint has returned arrays with different lengths (for example
    # time=262, hgt=262, sgt=35).  Truncating to the shortest array silently
    # pairs different timestamps and creates a false "latest" total.  Treat
    # that as a schema failure and let the caller omit the panel.
    if not times or len(times) != len(hgt) or len(times) != len(sgt):
        return None

    def _flist(lst):
        out = []
        for value in lst:
            if value in (None, ""):
                return None
            try:
                out.append(float(value))
            except (TypeError, ValueError):
                return None
        return out

    hgt_values = _flist(hgt)
    sgt_values = _flist(sgt)
    if hgt_values is None or sgt_values is None:
        return None
    return {
        "time": times, "hgt": hgt_values, "sgt": sgt_values,
        "latest_hgt": hgt_values[-1], "latest_sgt": sgt_values[-1],
        "latest_total": hgt_values[-1] + sgt_values[-1],
        "points": len(times), "_src": "ths",
    }


def _from_ths_hot_reason(date=None):
    """同花顺当日强势股 + 题材归因 reason（独家热点数据）。
    源 zx.10jqka.com.cn，免 token，沙箱出口被限（本机可用）。
    date='YYYY-MM-DD'，None=今天。返回 list[dict]（code/name/reason/close/pct/...）。"""
    import urllib.request as _u
    from datetime import date as _date
    if date is None:
        date = _date.today().strftime("%Y-%m-%d")
    url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
           f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
    req = _u.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"})
    try:
        raw = _u.urlopen(req, timeout=12).read()
        d = json.loads(_auto_decode(raw))
    except Exception:
        return None
    if d.get("errocode", 0) != 0:
        return None
    rows = d.get("data") or []
    if not rows:
        return None
    out = []
    for x in rows:
        out.append({
            "code": x.get("code"), "name": x.get("name"), "reason": x.get("reason"),
            "close": x.get("close"), "change": x.get("zhangdie"), "pct": x.get("zhangfu"),
            "turnover": x.get("huanshou"), "amount": x.get("chengjiaoe"),
            "volume": x.get("chengjiaoliang"), "dde": x.get("ddejingliang"),
            "market": x.get("market"), "_src": "ths",
        })
    return out


def _from_ths_eps_forecast(code):
    """同花顺机构一致预期（EPS / 净利润预测），解析 basic.10jqka.com.cn worth.html 表格。
    免 token，沙箱出口被限（本机可用）。无 pandas 依赖，纯正则解析。
    返回 dict：eps_forecast / netprofit_forecast（年度/机构数/最小/均值/最大/行业均值）
    + latest_year / latest_eps_mean 等汇总。"""
    import urllib.request as _u
    mkt, pure = _norm_code(code)
    url = f"https://basic.10jqka.com.cn/new/{pure}/worth.html"
    req = _u.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://basic.10jqka.com.cn/"})
    try:
        raw = _u.urlopen(req, timeout=15).read()
        html = _auto_decode(raw)
    except Exception:
        return None

    # 把 HTML 标签替换成空格，避免 <td>2026</td><td>46</td> 拼接成 "202646"...
    seg_all = re.sub(r"<[^>]+>", " ", html)
    seg_all = re.sub(r"\s+", " ", seg_all)

    def _parse_block(start_marker, end_marker):
        i = seg_all.find(start_marker)
        if i < 0:
            return []
        seg = seg_all[i:]
        # 注意：start_marker 不能只是 "每股收益"（它是 "预测年报每股收益" 的子串），
        # 否则会被自身的 "预测年报" 截断；必须用完整短语做边界。
        if end_marker:
            j = seg.find(end_marker)
            if j > 0:
                seg = seg[:j]
        rows = []
        # 行格式：4位年 + 5个数值（机构数 最小 均值 最大 行业平均）
        for m in re.finditer(r"(\d{4})\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", seg):
            nums = [float(m.group(k)) for k in range(2, 7)]
            rows.append({"year": m.group(1), "institutions": int(nums[0]),
                         "min": nums[1], "mean": nums[2], "max": nums[3],
                         "industry_avg": nums[4]})
        return rows

    eps = _parse_block("预测年报每股收益", "预测年报净利润")
    npf = _parse_block("预测年报净利润", "")
    if not eps and not npf:
        return None
    latest = eps[0] if eps else None
    return {
        "code": pure,
        "eps_forecast": eps,
        "netprofit_forecast": npf,
        "latest_year": latest["year"] if latest else None,
        "latest_eps_mean": latest["mean"] if latest else None,
        "latest_eps_min": latest["min"] if latest else None,
        "latest_eps_max": latest["max"] if latest else None,
        "_src": "ths",
    }


def _from_ths_limit_up(date):
    params = {"page": 1, "limit": 200, "field": "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004",
              "filter": "HS,GEM2STAR", "order_field": "330324", "order_type": "0", "date": date}
    try:
        d = _em_get_json("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool", params,
                         headers={"User-Agent": UA}, timeout=12)
        info = (d.get("data") or {}).get("info", [])
    except Exception:
        return None
    from datetime import datetime
    out = []
    for it in info:
        ft = it.get("first_limit_up_time")
        out.append({"code": it.get("code"), "name": it.get("name"), "price": it.get("latest"),
                    "pct": it.get("change_rate"), "reason": it.get("reason_type", ""),
                    "board_type": it.get("limit_up_type", ""), "seal_rate": it.get("limit_up_suc_rate"),
                    "break_times": it.get("open_num") or 0, "seal_amount": it.get("order_amount"),
                    "high_days": it.get("high_days", ""),
                    "first_time": datetime.fromtimestamp(int(ft)).strftime("%H:%M:%S") if ft else "",
                    "is_again": it.get("is_again_limit")})
    return out or None


def _from_ths_hot_list(period="hour"):
    try:
        d = _em_get_json("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
                         params={"stock_type": "a", "type": period, "list_type": "normal"},
                         headers={"User-Agent": UA}, timeout=10)
        lst = (d.get("data") or {}).get("stock_list") or []
    except Exception:
        return None
    out = []
    for it in lst:
        tag = it.get("tag") or {}
        out.append({"rank": it.get("order"), "code": it.get("code"), "name": it.get("name"),
                    "heat": it.get("rate"), "pct": it.get("rise_and_fall"),
                    "rank_chg": it.get("hot_rank_chg"), "concepts": tag.get("concept_tag") or [],
                    "tag": tag.get("popularity_tag", "")})
    return out or None


# ---------------------------------------------------------------- 同花顺融资融券 / 股东户数（第二源）
# 注意：同花顺 dataapi 对这两类暂无公开的免令牌端点（实测 404/空），此处按最可能结构编写并
# 防御解析，上线前需用真实样本校准端点与字段；失败优雅返回 None，由东财主源兜底。
def _from_ths_margin_trading(code, date=None):
    d = date or _d("%Y-%m-%d")
    try:
        d2 = _em_get_json("https://datacenter-web.10jqka.com.cn/api/margin/financing/detail",
                          params={"date": d, "stock": code, "pageSize": 30},
                          headers={"User-Agent": UA}, timeout=12)
        rows = (d2.get("data") or {}).get("list") or d2.get("list") or []
    except Exception:
        return None
    out = []
    for r in rows:
        out.append({"date": r.get("date") or r.get("tradingDay") or d,
                    "margin_balance": _f(r, "rzrqye") or _f(r, "margin_balance"),
                    "buy_repay": _f(r, "rzmre") or _f(r, "buy_repay"),
                    "sell_repay": _f(r, "rzmcle") or _f(r, "sell_repay"),
                    "short_balance": _f(r, "rqyl") or _f(r, "short_balance"),
                    "sec_balance": _f(r, "rqmcl") or _f(r, "sec_balance")})
    return out or None


def _from_ths_holder_num(code):
    try:
        d2 = _em_get_json("https://datacenter-web.10jqka.com.cn/api/stock/holder_num",
                          params={"stock": code, "pageSize": 20},
                          headers={"User-Agent": UA}, timeout=12)
        rows = (d2.get("data") or {}).get("list") or d2.get("list") or []
    except Exception:
        return None
    out = []
    for r in rows:
        out.append({"date": r.get("date") or r.get("endDate"),
                    "holder_num": _f(r, "holderNum") or _f(r, "holder_num"),
                    "hold_ratio": _f(r, "holdRatio") or _f(r, "hold_ratio"),
                    "chg": _f(r, "holderNumChg") or _f(r, "chg")})
    return out or None
