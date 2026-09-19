

"""Retained Sina financial and option adapters."""
from trade_system.http_transport import read_verified_once

import json
import urllib.request as _u
from trade_system.adapters.kline_sources import UA, _norm_code


def _from_sina_fund_flow(code, days=10):
    """新浪个股资金流（日度四档单净额）——东财被封时降级源，免 token。"""
    import urllib.request as _u
    mkt, pure = _norm_code(code)
    pre = ("sh" if mkt == "sh" else "bj" if mkt == "bj" else "sz") + pure
    u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}")
    req = _u.Request(u, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
    arr = json.loads(read_verified_once(req, timeout=15, max_bytes=4_000_000).decode("utf-8", "ignore"))
    out = []
    for x in arr:
        out.append({"date": str(x.get("opendate", ""))[:10],
                    "close": float(x.get("trade") or 0),
                    "net_amount": float(x.get("netamount") or 0),
                    "turnover": float(x.get("turnover") or 0),
                    "_src": "sina_fund_flow"})
    return out or None

def get_financial_statements(code, report_type="lrb", periods=8):
    """新浪财报三表（免 token，沙箱实测可用）——补东方财富 datacenter 无此表(资产负债/现金流)的缺口。
    report_type: 'lrb'利润表 / 'fzb'资产负债表 / 'llb'现金流量表
    返回 list[dict]，每期一条（键为科目名，含 '_同比' 键）。"""
    import urllib.request as _u, urllib.parse as _up
    mkt, pure = _norm_code(code)
    pre = "sh" if mkt == "sh" else "sz"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    p = {"paperCode": f"{pre}{pure}", "source": report_type, "type": "0",
         "page": "1", "num": str(periods)}
    req = _u.Request(url + "?" + _up.urlencode(p), headers={"User-Agent": UA})
    d = json.loads(read_verified_once(req, timeout=15, max_bytes=4_000_000))
    rl = ((d.get("result") or {}).get("data") or {}).get("report_list") or {}
    rows = []
    for per in sorted(rl.keys(), reverse=True)[:periods]:
        obj = rl[per]
        rec = {"报告期": f"{per[:4]}-{per[4:6]}-{per[6:8]}"}
        for it in (obj.get("data") or []):
            t = it.get("item_title", "")
            if not t or it.get("item_value") is None:
                continue
            rec[t] = it.get("item_value")
            tb = it.get("item_tongbi")
            if tb not in (None, ""):
                rec[t + "_同比"] = tb
        rows.append(rec)
    return rows or None

SINA_OPT_HDR = {"Referer": "https://stock.finance.sina.com.cn/", "User-Agent": UA}

def _sina_opt_list(param):
    try:
        req = _u.Request(f"https://hq.sinajs.cn/list={param}", headers=SINA_OPT_HDR)
        t = read_verified_once(req, timeout=10, max_bytes=4_000_000).decode("gbk", "ignore")
        return t.split('"')[1].split(",") if '"' in t else []
    except Exception:
        return []

def _opt_f(x):
    try:
        return float(x)
    except Exception:
        return x

def _from_sina_option_tquote(code):
    v = _sina_opt_list(f"CON_OP_{code}")
    if len(v) < 43:
        return None
    return {"bid_vol": _opt_f(v[0]), "bid": _opt_f(v[1]), "last": _opt_f(v[2]), "ask": _opt_f(v[3]),
            "ask_vol": _opt_f(v[4]), "open_interest": _opt_f(v[5]), "pct": _opt_f(v[6]),
            "strike": _opt_f(v[7]), "prev_close": _opt_f(v[8]), "open": _opt_f(v[9]),
            "limit_up": _opt_f(v[10]), "limit_down": _opt_f(v[11]), "name": v[37],
            "amplitude": _opt_f(v[38]), "high": _opt_f(v[39]), "low": _opt_f(v[40]),
            "volume": _opt_f(v[41]), "amount": _opt_f(v[42]), "_src": "sina"}

def _from_sina_option_greeks(code):
    raw = _sina_opt_list(f"CON_SO_{code}")
    if len(raw) < 16:
        return None
    v = [raw[0]] + raw[4:]  # raw[1:4] 是 3 个空串，必须跳过否则字段错位
    return {"name": v[0], "volume": _opt_f(v[1]), "delta": _opt_f(v[2]), "gamma": _opt_f(v[3]),
            "theta": _opt_f(v[4]), "vega": _opt_f(v[5]), "iv": _opt_f(v[6]), "high": _opt_f(v[7]),
            "low": _opt_f(v[8]), "trade_code": v[9], "strike": _opt_f(v[10]),
            "last": _opt_f(v[11]), "theory": _opt_f(v[12]), "_src": "sina"}
