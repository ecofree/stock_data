

"""Retained CNInfo adapters; acquisition policy is owned by resilient_sources."""
from trade_system.http_transport import read_verified_once, request_budget, request_deadline

import json
import os
import subprocess
import time
import sys
import urllib.request as _u
import urllib.parse as _up
from trade_system.adapters.kline_sources import UA
from trade_system.adapters._shared import _f


_CNINFO_ORGID_MAP = {}

def _cninfo_ts_to_date(ts):
    if isinstance(ts, (int, float)):
        return __import__("datetime").datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    return str(ts)[:10] if ts else ""

def _cninfo_orgid(code):
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            r = read_verified_once(_u.Request("https://www.cninfo.com.cn/new/data/szse_stock.json",
                                      headers={"User-Agent": UA}), timeout=15, max_bytes=4_000_000)
            _CNINFO_ORGID_MAP = {s["code"]: s["orgId"] for s in json.loads(r).get("stockList", [])}
        except Exception:
            pass
    org = _CNINFO_ORGID_MAP.get(code)
    if org:
        return org
    if code.startswith("6"):
        return f"gssh0{code}"
    elif code.startswith(("8", "4")):
        return f"gsbj0{code}"
    return f"gssz0{code}"

@request_budget(15)
def _from_cninfo_announcements(code, page_size=30):
    org_id = _cninfo_orgid(code)
    payload = {"stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": str(page_size),
               "pageNum": "1", "column": "", "category": "", "plate": "", "seDate": "",
               "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}
    try:
        req = _u.Request("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                         data=_up.urlencode(payload).encode(),
                         headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                                  "Referer": "https://www.cninfo.com.cn/new/disclosure",
                                  "Origin": "https://www.cninfo.com.cn"}, method="POST")
        d = json.loads(read_verified_once(req, timeout=15, max_bytes=4_000_000))
    except Exception:
        return None
    out = [{"id": str(it.get("announcementId") or ""), "title": it.get("announcementTitle", ""), "type": it.get("announcementTypeName", ""),
            "date": _cninfo_ts_to_date(it.get("announcementTime")),
            "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={it.get('announcementId', '')}"}
           for it in (d.get("announcements") or [])]
    return out or None

@request_budget(10)
def _from_cninfo_irm(code, page_size=30):
    try:
        r1 = read_verified_once(_u.Request("https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
                                   data=_up.urlencode({"keyWord": code}).encode(),
                                   headers={"User-Agent": UA}, method="POST"), timeout=10, max_bytes=4_000_000)
        d1 = json.loads(r1).get("data") or []
        if not d1:
            return None
        org_id = d1[0].get("secid")
        from datetime import datetime
        params = {"_t": 1, "stockcode": code, "orgId": org_id, "pageSize": page_size,
                  "pageNum": 1, "keyWord": "", "startDay": "", "endDay": ""}
        r2 = read_verified_once(_u.Request("https://irm.cninfo.com.cn/newircs/company/question?" + _up.urlencode(params),
                                   headers={"User-Agent": UA}, method="POST"), timeout=10, max_bytes=4_000_000)
        rows = json.loads(r2).get("rows") or []
    except Exception:
        return None
    out = []
    for it in rows:
        pd_ = it.get("pubDate")
        out.append({"code": it.get("stockCode"), "company": it.get("companyShortName"),
                    "question": it.get("mainContent"), "answer": it.get("attachedContent"),
                    "answerer": it.get("attachedAuthor"),
                    "ask_time": datetime.fromtimestamp(pd_ / 1000).strftime("%Y-%m-%d %H:%M") if pd_ else ""})
    return out or None

@request_budget(10)
def _cninfo_enckey():
    p = os.environ.get("CNINFO_JS")
    if not p:
        cand = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cninfo.js")
        p = cand if os.path.exists(cand) else None
    if not p or not os.path.exists(p):
        return None
    try:
        if p.endswith(".js"):
            remaining = request_deadline.get() - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('CNINFO token deadline exhausted')
            out = subprocess.run(['node', '-e',
                "const fs=require('fs');eval(fs.readFileSync(process.argv[1],'utf8'));"
                "process.stdout.write(String(getResCode1()).slice(0,4097))", p],
                capture_output=True, timeout=remaining,
                creationflags=0x08000000 if sys.platform == 'win32' else 0)
            if out.returncode or len(out.stdout) > 4096:
                return None
            return out.stdout.decode('utf-8').strip() or None
        with open(p, encoding='utf-8') as fh:
            token = fh.read(4097)
        return token.strip() or None if len(token) <= 4096 else None
    except subprocess.TimeoutExpired:
        raise TimeoutError('CNINFO token deadline exhausted') from None
    except TimeoutError:
        raise
    except Exception:
        return None


@request_budget(15)
def _cninfo_webapi(api, params):
    """取 cninfo webapi 的 records；无令牌/失败返回 None。"""
    enc = _cninfo_enckey()
    if not enc:
        return None
    try:
        url = "https://webapi.cninfo.com.cn/api/sysapi/" + api
        req = _u.Request(url + "?" + _up.urlencode(params),
                         headers={"User-Agent": UA, "Accept-Enckey": enc,
                                  "Accept": "*/*", "Referer": "http://webapi.cninfo.com.cn/"},
                         method="GET")
        d = json.loads(read_verified_once(req, timeout=15, max_bytes=4_000_000))
    except Exception:
        return None
    return d.get("records") or d.get("data") or []

def _from_cninfo_dividend(code):
    """巨潮分红配股第二源（p_sysapi1139）。字段已按 akshare 实测命名映射。"""
    recs = _cninfo_webapi("p_sysapi1139", {"scode": code})
    if not recs:
        return None
    out = [{"report_date": r.get("F001V", ""), "impl_date": r.get("F006D", ""),
            "div_type": r.get("F044V", ""), "bonus_ratio": _f(r, "F011N"),
            "transfer_ratio": _f(r, "F010N"), "cash_div": _f(r, "F012N"),
            "record_date": r.get("F018D", ""), "ex_date": r.get("F020D", ""),
            "pay_date": r.get("F023D", ""), "desc": r.get("F007V", "")}
           for r in recs]
    return out or None

def _from_cninfo_lockup(code):
    """巨潮限售股解禁第二源（p_sysapi1011）。字段名上线前需用真实样本校准。"""
    recs = _cninfo_webapi("p_sysapi1011", {"scode": code})
    if not recs:
        return None
    out = [{"ann_date": r.get("F001D", r.get("DECLAREDATE", "")),
            "float_date": r.get("F002D", ""), "float_shares": _f(r, "F003N"),
            "float_ratio": _f(r, "F004N"), "holder": r.get("F005V", ""),
            "share_type": r.get("F006V", "")}
           for r in recs]
    return out or None

def _from_cninfo_block_trade(code):
    """巨潮大宗交易第二源（p_sysapi1009）。字段名上线前需用真实样本校准。"""
    recs = _cninfo_webapi("p_sysapi1009", {"scode": code})
    if not recs:
        return None
    out = [{"date": r.get("F001D", ""), "price": _f(r, "F002N"),
            "volume": _f(r, "F003N"), "amount": _f(r, "F004N"),
            "buyer": r.get("F005V", ""), "seller": r.get("F006V", "")}
           for r in recs]
    return out or None
