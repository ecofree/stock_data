"""
统一 A 股数据入口 —— 沙箱多源容灾（每源带超时 + 自动降级）

设计要点：把数据源按「基础设施类别」分层，避免所有源都依赖同一类被封域名。
  ├─ 独立基础设施（最不易被沙箱限掉，优先）
  │   ├─ baostock      : 直连 baostock 自有服务器（HTTP，但域名独立）
  │   └─ pytdx / mootdx: TCP 直连【交易所/通达信行情服务器】(115.238.90.165:7709 等)，
  │                      完全不经过 eastmoney / sina / tencent 的 HTTP 接口
  ├─ HTTP 行情源（可能被逐个限掉，作兜底）
  │   ├─ 腾讯财经 qt.gtimg.cn
  │   ├─ 新浪财经 money.finance.sina.com.cn
  │   ├─ 东方财富 push2his（仅在本机/非沙箱可达；沙箱内自动跳过）
  │   └─ Tushare 中继 fastapic.stockai888.top
  └─ 财务/资金流
      ├─ 东方财富 datacenter-web / datacenter（沙箱 200 可用，免 token）业绩报表+资金流
      ├─ 新浪财报三表 quotes.sina.cn（资产负债表/利润表/现金流量表，免 token，补 datacenter 无此表的缺口）
      ├─ 新浪资金流 vip.stock.finance.sina.com.cn（东财被封时降级）
      └─ Tushare 中继（全量字段）

K 线源另含：百度股市通 finance.pae.baidu.com（独立域名，自带 MA）。

运行环境：需在已安装 pytdx / mootdx / baostock / requests 的 venv 中执行
  （venv/Scripts/python.exe stock_data.py ...）

调用示例：
  from trade_system.stock_data_sources import get_kline, get_financials, get_fund_flow, get_financial_statements
  bars = get_kline("600519", "20260701", "20260710")              # 自动多源降级
  fin  = get_financials("600519")                                  # 业绩报表(东方财富)
  ff   = get_fund_flow("600519")                                   # 资金流(东方财富→新浪兜底)
  bs   = get_financial_statements("600519", "fzb")                 # 资产负债表(新浪)
"""
from __future__ import annotations
import subprocess, json, socket, os
import datetime
import urllib.request as _u, urllib.parse as _up  # noqa: E402

from trade_system.logging_setup import get_logger

logger = get_logger(__name__)

from trade_system.adapters._shared import _f, _d  # noqa: F401

# Facade re-exports: source implementations moved to adapters/, every
# historical name stays importable from here.
from trade_system.adapters.kline_sources import (  # noqa: F401
    _BAOSTOCK_LOCK,
    _BAOSTOCK_MODULE,
    _BAOSTOCK_LOGGED_IN,
    _close_baostock,
    UA,
    EM_UT,
    TUSHARE_RELAY,
    TUSHARE_TOKEN,
    TDX_HOSTS,
    _run,
    _norm_code,
    _norm_date,
    _yymmdd,
    _curl_json,
    _auto_decode,
    _from_baostock,
    _from_pytdx,
    _from_tencent,
    _from_sina,
    _from_eastmoney,
    _from_baidu,
    _from_tushare_relay,
    _tushare_query,
    _from_tushare_moneyflow,
    _from_tushare_sector_flow,
    _KLINE_SOURCES,
    get_kline,
)
from trade_system.adapters.eastmoney_dc import (  # noqa: F401
    DATACENTER_URL,
    EM_SESSION_HDR,
    _em_get_json,
    _em_get_clist_json,
    _em_datacenter,
    _em_zt_api,
    _fmt_zt_time,
    _from_em_dragon_tiger,
    _from_em_dragon_tiger_daily,
    _from_em_margin,
    _from_em_holder,
    _from_em_lockup,
    _from_em_dividend,
    _from_em_block_trade,
    _from_em_fund_flow_120d,
    _from_em_stock_info,
    _from_em_industry_rank,
    _from_em_sector_flow,
    _from_em_sector_flow_page,
    _from_em_zt_pool,
    _from_em_zb_pool,
    _from_em_dt_pool,
    _from_em_yzt_pool,
    _from_em_limit_up_sentiment,
    _from_em_reports,
    _from_em_stock_news,
    _from_em_flash,
    _from_cls_telegraph,
)
from trade_system.adapters.ths_sources import (  # noqa: F401
    _from_ths_northbound,
    _from_ths_hot_reason,
    _from_ths_eps_forecast,
    _from_ths_limit_up,
    _from_ths_hot_list,
    _from_ths_margin_trading,
    _from_ths_holder_num,
)

try:
    from trade_system.config import SETTINGS as _PROJECT_SETTINGS
except Exception:
    _PROJECT_SETTINGS = {}

# 防止任何源在沙箱内卡死：全局 socket 超时
socket.setdefaulttimeout(10)


# ---------------------------------------------------------------- 7) 腾讯实时行情 qt.gtimg.cn（批量，含 PE/PB/市值/涨跌停）
def _qt_code(code):
    """返回 qt.gtimg.cn 用的 sh600519 / sz000001 / bj8xxxxx 形式。"""
    mkt, pure = _norm_code(code)
    pre = "sh" if mkt == "sh" else "bj" if mkt == "bj" else "sz"
    return pre + pure


def _from_tencent_quote(codes):
    """批量实时行情（qt.gtimg.cn）。codes 可为纯代码或带市场前缀；返回 {纯代码: [~分隔字段列表]}。
    字段索引见 _TENCENT_Q / _from_tencent_valuation。"""
    if isinstance(codes, str):
        codes = [codes]
    q = ",".join(_qt_code(c) for c in codes)
    out = subprocess.run(
        ["curl", "-s", "-4", "--compressed", "-m", "12", "-H", f"User-Agent: {UA}",
         f"https://qt.gtimg.cn/q={q}"], capture_output=True, timeout=20)
    # 注意：腾讯行情返回 GBK 编码（含中文股票名），必须按字节读再 gbk 解码，
    # 不能 text=True（utf-8 会 UnicodeDecodeError 导致整条取值失败）。
    text = out.stdout.decode("gbk", "ignore")
    result = {}
    for line in text.split(";"):
        if "=" not in line:
            continue
        _, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        if not payload:
            continue
        parts = payload.split("~")
        if len(parts) < 49:
            continue
        code = parts[2] if len(parts) > 2 else ""
        if not code:
            continue
        result[code] = parts
    return result or None


# 腾讯 qt.gtimg.cn 字段索引（2026-07-12 实测 sh600519 / sz000001 校准：茅台总市值 15063 亿、平安 2027 亿吻合）
_TENCENT_Q = {
    "name": 1, "code": 2, "price": 3, "pre_close": 4, "open": 5,
    "volume": 6, "change": 31, "change_pct": 32, "high": 33, "low": 34,
    "amount_wan": 37, "turnover": 38, "pe_ttm": 39, "vol_ratio": 43,
    "total_mv_yi": 44, "circ_mv_yi": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48, "time": 30,
}


def _from_tencent_valuation(code):
    """个股估值 / 实时快照：价量 + PE(TTM) + PB + 总/流通市值(亿) + 涨跌停价。
    腾讯 qt.gtimg.cn，免 token，沙箱实测 200 可用。"""
    d = _from_tencent_quote([code])
    if not d:
        return None
    parts = next(iter(d.values()))
    if not parts:
        return None

    def g(k):
        try:
            v = parts[_TENCENT_Q[k]]
            return float(v) if v not in ("", None) else None
        except (ValueError, IndexError):
            return None

    return {
        "code": parts[2],
        "name": parts[1],
        "price": g("price"), "pre_close": g("pre_close"), "open": g("open"),
        "high": g("high"), "low": g("low"), "volume": g("volume"),
        "amount": g("amount_wan"), "change": g("change"), "change_pct": g("change_pct"),
        "turnover": g("turnover"), "pe_ttm": g("pe_ttm"), "pb": g("pb"),
        "total_mv": g("total_mv_yi"), "circ_mv": g("circ_mv_yi"),
        "limit_up": g("limit_up"), "limit_down": g("limit_down"),
        "time": parts[_TENCENT_Q["time"]], "_src": "tencent",
    }


# ---------------------------------------------------------------- 8) 股票列表参考镜像（Tushare 中继）
def _from_tushare_basic(list_status="L"):
    """全量股票列表（ts_code/name/industry/market/list_date），Tushare 中继，用于本地参考镜像（总量容灾）。"""
    return _tushare_query(
        "stock_basic", {"list_status": list_status},
        fields="ts_code,symbol,name,industry,market,list_date",
    )


# ---------------------------------------------------------------- 9) 同花顺（独立域名组：hexin.cn / 10jqka.com.cn）—— 零 token


# ---------------------------------------------------------------- 统一入口：K 线


# ---------------------------------------------------------------- 财务 / 资金流（东方财富 datacenter）
def get_financials(code, periods=8):
    """业绩报表（营收/归母净利/EPS/同比），东方财富 datacenter，沙箱可用。
    返回统一结构 list[dict]：date/revenue/net_profit/eps/revenue_yoy。"""
    try:
        from .eastmoney_finance import get_income_statement
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
    """个股资金流：优先东方财富 datacenter（沙箱可用），失败则降级新浪资金流。
    返回统一结构 list[dict]：date/close/pct/super_net/big_net（东财）或 date/close/net_amount/turnover（新浪）。"""
    try:
        from .eastmoney_finance import get_fund_flow as _ff
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
    # 东财被封 → 新浪资金流兜底
    return _run(lambda: _from_sina_fund_flow(code, periods), timeout=15)


def _from_sina_fund_flow(code, days=10):
    """新浪个股资金流（日度四档单净额）——东财被封时降级源，免 token。"""
    import urllib.request as _u
    mkt, pure = _norm_code(code)
    pre = ("sh" if mkt == "sh" else "bj" if mkt == "bj" else "sz") + pure
    u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}")
    req = _u.Request(u, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
    arr = json.loads(_u.urlopen(req, timeout=15).read().decode("utf-8", "ignore"))
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
    import urllib.request as _u, urllib.parse as _up, ssl
    mkt, pure = _norm_code(code)
    pre = "sh" if mkt == "sh" else "sz"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    p = {"paperCode": f"{pre}{pure}", "source": report_type, "type": "0",
         "page": "1", "num": str(periods)}
    req = _u.Request(url + "?" + _up.urlencode(p), headers={"User-Agent": UA})
    d = json.loads(_u.urlopen(req, timeout=15, context=ctx).read())
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


# ================================================================ 东财系统一请求（urllib，无 requests 依赖）


# ---------------------------------------------------------------- 巨潮公告 + 互动易
_CNINFO_ORGID_MAP = {}


def _cninfo_ts_to_date(ts):
    if isinstance(ts, (int, float)):
        return __import__("datetime").datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    return str(ts)[:10] if ts else ""


def _cninfo_orgid(code):
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            r = _u.urlopen(_u.Request("http://www.cninfo.com.cn/new/data/szse_stock.json",
                                      headers={"User-Agent": UA}), timeout=15).read()
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
        d = json.loads(_u.urlopen(req, timeout=15).read())
    except Exception:
        return None
    out = [{"title": it.get("announcementTitle", ""), "type": it.get("announcementTypeName", ""),
            "date": _cninfo_ts_to_date(it.get("announcementTime")),
            "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={it.get('announcementId', '')}"}
           for it in (d.get("announcements") or [])]
    return out or None


def _from_cninfo_irm(code, page_size=30):
    try:
        r1 = _u.urlopen(_u.Request("https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
                                   data=_up.urlencode({"keyWord": code}).encode(),
                                   headers={"User-Agent": UA}, method="POST"), timeout=10).read()
        d1 = json.loads(r1).get("data") or []
        if not d1:
            return None
        org_id = d1[0].get("secid")
        from datetime import datetime
        params = {"_t": 1, "stockcode": code, "orgId": org_id, "pageSize": page_size,
                  "pageNum": 1, "keyWord": "", "startDay": "", "endDay": ""}
        r2 = _u.urlopen(_u.Request("https://irm.cninfo.com.cn/newircs/company/question?" + _up.urlencode(params),
                                   headers={"User-Agent": UA}, method="POST"), timeout=10).read()
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


# ---------------------------------------------------------------- 巨潮 webapi 第二源（需动态令牌）
# cninfo 的 webapi 现强制 Accept-Enckey 动态令牌；老门户 www.cninfo.com.cn/new 免令牌但只暴露
# 公告/互动易。本组函数用 webapi 取分红/解禁/大宗，作为东财主源的异后端第二源。
# 激活方式（零 Python 依赖，用环境自带 node）：
#   把 akshare 的 cninfo.js（含 getResCode1）放到本目录，或设置环境变量 CNINFO_JS 指向它；
#   或把"已算好的令牌字符串"写入某文件并设 CNINFO_JS 指向该文件（测试用）。
# 未配置时 _cninfo_enckey 返回 None → 本组函数优雅返回 None，由东财主源兜底（永不空手）。
def _cninfo_enckey():
    p = os.environ.get("CNINFO_JS")
    if not p:
        cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cninfo.js")
        p = cand if os.path.exists(cand) else None
    if not p or not os.path.exists(p):
        return None
    try:
        if p.endswith(".js"):
            out = subprocess.run(["node", "-e",
                                  "const fs=require('fs');eval(fs.readFileSync(%r,'utf8'));"
                                  "process.stdout.write(String(getResCode1()))" % p],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None
        with open(p, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except Exception:
        return None


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
        d = json.loads(_u.urlopen(req, timeout=15).read())
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


# ---------------------------------------------------------------- 同花顺涨停揭秘 + 热榜


# ---------------------------------------------------------------- 东财人气榜 + 个股热门概念
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


# ---------------------------------------------------------------- 新浪 ETF 期权（T型报价 + 希腊字母 + IV）
SINA_OPT_HDR = {"Referer": "https://stock.finance.sina.com.cn/", "User-Agent": UA}


def _sina_opt_list(param):
    try:
        req = _u.Request(f"https://hq.sinajs.cn/list={param}", headers=SINA_OPT_HDR)
        t = _u.urlopen(req, timeout=10).read().decode("gbk", "ignore")
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


# ---------------------------------------------------------------- 本地估值指标（零网络，依赖已缓存的 valuation + consensus_eps）
def _from_local_valuation_metrics(code):
    """前向PE / PEG / PE消化年数 —— 纯本地计算，不发起网络请求。
    输入：腾讯实时价 + 同花顺一致预期EPS（若取不到则跳过对应指标）。"""
    val = _from_tencent_valuation(code)
    if not val:
        return None
    price = val.get("price")
    eps = _from_ths_eps_forecast(code)
    out = {"code": val.get("code"), "name": val.get("name"), "price": price,
           "pe_ttm": val.get("pe_ttm"), "pb": val.get("pb"), "_src": "local"}
    if eps and eps.get("latest_eps_mean"):
        fwd = eps["latest_eps_mean"]
        out["forward_pe"] = round(price / fwd, 2) if fwd > 0 else None
        cur = val.get("pe_ttm")
        if cur and fwd and fwd > 0:
            cagr = (fwd / (price / cur) - 1) if (price / cur) > 0 else 0  # 下年EPS/当年EPS - 1
            out["cagr"] = round(cagr, 4)
            out["peg"] = round((price / fwd) / (cagr * 100), 2) if cagr > 0 else None
            out["pe_digestion_years"] = round(__import__("math").log(cur / 30) / __import__("math").log(1 + cagr), 2) if (cur > 30 and cagr > 0) else 0.0
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--start", default="20260701")
    ap.add_argument("--end", default="20260710")
    ap.add_argument("--fq", default="qfq")
    a = ap.parse_args()
    for c in a.codes:
        bars = get_kline(c, a.start, a.end, a.fq)
        src = bars[0]["_src"] if bars else "NONE"
        print(f"\n=== {c}  K线来源: {src}  共 {len(bars)} 根 ===")
        for b in bars[:3]:
            print(f"  {b['date']} O{b['open']} C{b['close']} H{b['high']} L{b['low']} V{b['volume']}")
        fin = get_financials(c)
        if fin:
            f0 = fin[0]
            print(f"  业绩: {f0.get('date')} 营收{f0.get('revenue')} 归母{f0.get('net_profit')} EPS{f0.get('eps')} 同比{f0.get('revenue_yoy')}%")


# =====================================================================
# P0 补缺：分时（双源）+ 核心单源异后端第二源
# =====================================================================
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


def _from_pytdx_minutes(code, date=None):
    """分时（通达信）：需装 pytdx/mootdx。沙箱未装时优雅返回 None——东财 trends2 作主源。
    这是与东财 HTTP 完全异构的 TCP 后端，是分时的最强冗余。"""
    try:
        from pytdx.quotes import Quotes
    except Exception:
        try:
            from mootdx.quotes import Quotes
        except Exception:
            return None
    try:
        client = Quotes.factory(market="std")
        dt = date or datetime.date.today().strftime("%Y%m%d")
        df = client.minutes(symbol=code, date=dt)
        if df is None or len(df) == 0:
            return None
        recs = []
        for _, row in df.iterrows():
            recs.append({
                "time": str(row.get("time")),
                "price": float(row.get("price") or 0),
                "avg": float(row.get("avg_price", row.get("avg", 0)) or 0),
                "volume": float(row.get("volume", 0) or 0),
            })
        return {"code": code, "name": None, "date": dt, "trends": recs}
    except Exception:
        return None


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


# =====================================================================
# P1 补缺：覆盖面缺口（指数/ETF/可转债/业绩预告快报/十大股东/历史北向/五档盘口）
#         + 核心单源异后端第二源
# =====================================================================
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
    d = _curl_json(url, timeout=10)
    rows = ((d or {}).get("data") or {}).get("klines") or []
    out = []
    for line in rows:
        p = line.split(",")
        if len(p) < 7:
            continue
        out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                    "amount": float(p[6]), "pct": (float(p[8]) if len(p) > 8 else None),
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
            "total_mv": _f(d, "f116"), "time": d.get("f86"), "_src": "eastmoney"}


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


def _from_em_forecast(code, page_size=20):
    """业绩预告：东财数据中心（多候选 reportName 提升命中率）。"""
    data = _em_datacenter_try(
        ["RPT_LM_YJYG_DET", "RPT_YJYG_YJYS", "RPT_LM_YJYG_YJYS"],
        filter_str=f'(SECURITY_CODE="{_norm_code(code)[1]}")', page_size=page_size)
    if not data:
        return None
    out = []
    for r in data:
        out.append({"date": r.get("PERFORMANCE_PREDICT_DATE") or r.get("PREDICT_DATE"),
                    "type": r.get("PERFORMANCE_PREDICT_TYPE") or r.get("PREDICT_TYPE"),
                    "content": r.get("PERFORMANCE_PREDICT_CONTENT") or r.get("PREDICT_CONTENT"),
                    "change_pct": _f(r, "ADD_AMP_UPPER") or _f(r, "CHANGE_PCT"),
                    "net_profit_min": _f(r, "NET_PROFIT_MIN"),
                    "net_profit_max": _f(r, "NET_PROFIT_MAX")})
    return out or None


def _from_em_express(code, page_size=10):
    """业绩快报：东财数据中心。"""
    data = _em_datacenter_try(
        ["RPT_LM_YJBB_LATEST", "RPT_LM_YJBB", "RPT_F10_FYB"],
        filter_str=f'(SECURITY_CODE="{_norm_code(code)[1]}")', page_size=page_size)
    if not data:
        return None
    out = []
    for r in data:
        out.append({"date": r.get("NOTICE_DATE") or r.get("REPORT_DATE"),
                    "revenue": _f(r, "TOTAL_OPERATE_INCOME") or _f(r, "REVENUE"),
                    "revenue_yoy": _f(r, "OPERATE_INCOME_YOY"),
                    "net_profit": _f(r, "PARENT_NETPROFIT") or _f(r, "NET_PROFIT"),
                    "net_profit_yoy": _f(r, "PARENT_NETPROFIT_YOY"),
                    "eps": _f(r, "BASIC_EPS")})
    return out or None


def _from_em_top10_holders(code, page_size=10):
    """十大股东：东财数据中心（多候选 reportName）。"""
    data = _em_datacenter_try(
        ["RPT_F10_HOLDERS", "RPT_F10_FUND_HOLDERS", "RPT_F10_EH_FREEHOLDERS"],
        filter_str=f'(SECURITY_CODE="{_norm_code(code)[1]}")', page_size=page_size)
    if not data:
        return None
    out = []
    for r in data:
        out.append({"holder": r.get("HOLDER_NAME") or r.get("PORTFOLIO_NAME"),
                    "ratio": _f(r, "HOLD_RATIO") or _f(r, "PROPORTION"),
                    "shares": _f(r, "HOLD_NUM") or _f(r, "QUANTITY"),
                    "nature": r.get("HOLDER_TYPE") or r.get("TYPE"),
                    "end_date": r.get("END_DATE") or r.get("REPORT_DATE")})
    return out or None


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


def _from_tencent_bid_ask(code):
    """五档盘口（腾讯 qt.gtimg.cn）：买一~五 / 卖一~五 价格与量，零依赖、可靠。"""
    d = _from_tencent_quote([code])
    if not d:
        return None
    parts = next(iter(d.values()))
    if len(parts) < 29:
        return None
    def num(i):
        try:
            return float(parts[i]) if parts[i] not in ("", "-") else None
        except Exception:
            return None
    def lvl(p, v):
        return {"price": num(p), "volume": num(v)}
    return {
        "code": parts[2], "name": parts[1], "price": num(3),
        "buy": [lvl(9, 10), lvl(11, 12), lvl(13, 14), lvl(15, 16), lvl(17, 18)],
        "sell": [lvl(19, 20), lvl(21, 22), lvl(23, 24), lvl(25, 26), lvl(27, 28)],
        "_src": "tencent",
    }


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


# ---- 核心单源异后端第二源（东财系补位） ----
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


def _from_em_announcements(code):
    """公告第二源：东财公告（与巨潮 announcements 异后端）。"""
    data = _em_datacenter_try(
        ["RPT_ANNOUNCEMENT", "RPT_DMS_WEB_ADIS"],
        filter_str=f'(SECURITY_CODE="{_norm_code(code)[1]}")', page_size=20)
    if not data:
        return None
    out = []
    for r in data:
        out.append({"title": r.get("NOTICE_TITLE") or r.get("TITLE"),
                    "type": r.get("NOTICE_TYPE") or r.get("TYPE"),
                    "date": r.get("NOTICE_DATE") or r.get("DATE"),
                    "id": r.get("NOTICE_ID") or r.get("ID")})
    return out or None


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


def _from_em_option_greeks(code):
    """期权希腊字母第二源：东财期权（与新浪 sina_option 异后端）。"""
    # 东财期权没有直接的希腊字母字段，退化为报价 + 提示
    q = _from_em_option_tquote(code)
    if not q:
        return None
    q["delta"] = None; q["gamma"] = None; q["theta"] = None
    q["vega"] = None; q["rho"] = None; q["note"] = "东财期权仅报价，希腊字母请用 sina_option 源"
    return q


def _from_em_statements(code, report_type="lrb", periods=8):
    """三表第二源：东财财务报表（与新浪 statements 异后端）。"""
    table = {"lrb": "RPT_F10_FINANCE_MAIN", "fzb": "RPT_F10_FINANCE_MAIN",
             "llb": "RPT_F10_FINANCE_MAIN"}.get(report_type, "RPT_F10_FINANCE_MAIN")
    data = _em_datacenter_try([table],
                              filter_str=f'(SECURITY_CODE="{_norm_code(code)[1]}")',
                              page_size=periods * 4, timeout=15)
    if not data:
        return None
    out = []
    for r in data:
        out.append({"date": r.get("REPORT_DATE") or r.get("DATE"),
                    "report_type": report_type,
                    "total_revenue": _f(r, "TOTAL_OPERATE_INCOME") or _f(r, "OPERATE_INCOME"),
                    "net_profit": _f(r, "PARENT_NETPROFIT") or _f(r, "PARENT_NET_PROFIT"),
                    "total_assets": _f(r, "TOTAL_ASSETS"),
                    "total_liabilities": _f(r, "TOTAL_LIAB"),
                    "equity": _f(r, "TOTAL_EQUITY") or _f(r, "EQUITY")})
    return out or None


def _from_em_hot_topics():
    """热点题材第二源：东财题材榜（与同花顺 hot_topics 异后端）。"""
    params = {"pn": "1", "pz": "20", "po": "1", "np": "1",
              "ut": EM_UT, "fltt": "2", "invt": "2",
              "fs": "m:90+t:2", "fields": "f12,f14,f3,f62,f184,f66,f104"}
    d = _em_get_json("https://push2.eastmoney.com/api/qt/clist/get", params, timeout=12)
    diff = (d or {}).get("data") or {}
    rows = diff.get("diff") or []
    if not rows:
        return None
    return [{"code": r.get("f12"), "name": r.get("f14"), "pct": _f(r, "f3"),
             "hot": _f(r, "f62"), "rank": _f(r, "f104")} for r in rows]


def _from_em_hot_list(period="hour"):
    """人气榜第二源：东财人气榜（与同花顺 ths_hot_list 异后端）。"""
    return _from_em_hot_rank(50)  # 东财人气榜即 em_hot_rank，复用


# =====================================================================
# P2 补缺：新股/IPO 日历 + 宏观经济（覆盖面收尾）
# =====================================================================
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
