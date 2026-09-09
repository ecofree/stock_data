"""K-line source adapters with multi-source fallback.

Extracted verbatim from ``trade_system.stock_data_sources`` (now a facade).
All names remain importable from the facade for compatibility.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import atexit
import urllib.error as _ue  # noqa: F401

from trade_system.logging_setup import get_logger

try:
    from trade_system.config import SETTINGS as _PROJECT_SETTINGS
except Exception:
    _PROJECT_SETTINGS = {}

logger = get_logger(__name__)


# BaoStock is a process-level HTTP session.  Logging in/out for every stock
# made the historical fallback both slow and prone to server throttling.  A
# single locked session is reused by this process and closed at interpreter
# exit; query failures reset it so the next call can reconnect cleanly.
_BAOSTOCK_LOCK = threading.RLock()
_BAOSTOCK_MODULE = None
_BAOSTOCK_LOGGED_IN = False


def _close_baostock() -> None:
    global _BAOSTOCK_LOGGED_IN
    with _BAOSTOCK_LOCK:
        if _BAOSTOCK_MODULE is not None and _BAOSTOCK_LOGGED_IN:
            try:
                _BAOSTOCK_MODULE.logout()
            except Exception:
                pass
        _BAOSTOCK_LOGGED_IN = False


atexit.register(_close_baostock)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
# Tushare is an optional last-resort source.  Keep credentials out of source
# control; the non-Tushare providers remain fully usable when these are empty.
TUSHARE_RELAY = (
    os.environ.get("TUSHARE_FAST_RELAY_URL")
    or _PROJECT_SETTINGS.get("TUSHARE_FAST_RELAY_URL")
    or "https://fastapic.stockai888.top"
)
TUSHARE_TOKEN = (
    os.environ.get("TUSHARE_FAST_RELAY_TOKEN")
    or os.environ.get("TUSHARE_TOKEN")
    or _PROJECT_SETTINGS.get("TUSHARE_FAST_RELAY_TOKEN")
    or _PROJECT_SETTINGS.get("TUSHARE_TOKEN")
    or ""
)

# pytdx / mootdx 可达的交易所行情服务器（沙箱实测 TCP 7709 通，2026-07），合并两份清单自动轮询
TDX_HOSTS = [
    "115.238.90.165", "180.153.18.170", "218.108.98.36", "60.12.138.11",
    # 来自 a-stock-data 仓库实测可用清单（2026-06 验证，沙箱可达）
    "119.97.185.59", "124.70.133.119", "116.205.183.150", "123.60.73.44",
    "116.205.163.254", "121.36.225.169", "123.60.70.228", "124.71.9.153",
    "110.41.147.114", "124.71.187.122",
]


# ---------------------------------------------------------------- 工具
def _run(func, timeout=10, label=None):
    """在线程里跑源，超时/异常都返回 None，绝不阻塞主流程。"""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = executor.submit(func)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:
            logger.debug(
                "source task failed (%s): %r",
                label or getattr(func, "__name__", "?"), exc,
            )
            return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _norm_code(code):
    """返回 (mkt, pure) ；mkt in sh/sz/bj。"""
    c = code.strip().lower().replace(".", "")
    if c.startswith("sh"):
        return "sh", c[2:]
    if c.startswith("sz"):
        return "sz", c[2:]
    if c.startswith("bj"):
        return "bj", c[2:]
    if code.startswith("92"):
        return "bj", code
    if code[0] == "6":
        return "sh", code
    if code[0] in "03":
        return "sz", code
    if code[0] in "48":
        return "bj", code
    return "sh", code


def _norm_date(d):
    return re.sub(r"\D", "", d)[:8]


def _yymmdd(d):
    d = _norm_date(d)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _curl_json(url, timeout=12):
    out = subprocess.run(
        ["curl", "-s", "-4", "--compressed", "-m", str(timeout), "-H", f"User-Agent: {UA}", url],
        capture_output=True, text=True, timeout=timeout + 5)
    try:
        return json.loads(out.stdout)
    except Exception as exc:
        logger.debug("curl json parse failed for %s: %s; body=%r",
                     url, exc, (out.stdout or "")[:200])
        return None


def _auto_decode(raw):
    """同花顺等站点编码不统一（多为 GBK，少数 UTF-8）。utf-8 优先，
    若解码出现乱码替换符 \\ufffd（说明是 GBK 字节），则回退 GBK。"""
    try:
        t = raw.decode("utf-8")
        if "\ufffd" not in t:
            return t
    except Exception:
        pass
    return raw.decode("gbk", "ignore")



# ---------------------------------------------------------------- 1) baostock（独立服务器）
def _from_baostock(code, start, end, fq="qfq"):
    global _BAOSTOCK_MODULE, _BAOSTOCK_LOGGED_IN
    import baostock as bs
    _BAOSTOCK_MODULE = bs
    mkt, pure = _norm_code(code)
    # 注意：baostock 的 adjustflag="1"(前复权) 对部分标的返回异常放大值（实测 600519 返回 9066 而非 1182）；
    # 用 "3"不复权 原始价最稳，qfq 由 腾讯/新浪/Tushare 源补齐。
    adj = {"qfq": "3", "hfq": "2", "": "3"}[fq]
    with _BAOSTOCK_LOCK:
        if not _BAOSTOCK_LOGGED_IN:
            login = bs.login()
            if getattr(login, "error_code", "1") != "0":
                return None
            _BAOSTOCK_LOGGED_IN = True
        try:
            rs = bs.query_history_k_data_plus(
                f"{mkt}.{pure}", "date,open,high,low,close,volume,amount",
                start_date=_yymmdd(start), end_date=_yymmdd(end),
                frequency="d", adjustflag=adj)
            if rs.error_code != "0":
                return None
            rows = []
            while (rs.error_code == "0") & rs.next():
                r = rs.get_row_data()
                if not r or not r[0]:
                    continue
                rows.append({
                    "date": r[0], "open": float(r[1] or 0), "high": float(r[2] or 0),
                    "low": float(r[3] or 0), "close": float(r[4] or 0),
                    "volume": float(r[5] or 0), "amount": float(r[6] or 0),
                    "volume_unit": "shares", "amount_unit": "yuan",
                    "adjustment": "none", "_src": "baostock"})
            return rows or None
        except Exception:
            # The next request should perform a fresh login rather than reuse
            # a server-side session that has been closed or throttled.
            try:
                bs.logout()
            except Exception:
                pass
            _BAOSTOCK_LOGGED_IN = False
            return None


# ---------------------------------------------------------------- 2) pytdx（交易所 TCP 直连）
def _from_pytdx(code, start, end, fq="qfq"):
    from pytdx.hq import TdxHq_API
    mkt, pure = _norm_code(code)
    market = {"sh": 1, "sz": 0, "bj": 2}[mkt]
    api = TdxHq_API(raise_exception=False)
    sd, ed = _norm_date(start), _norm_date(end)
    for host in TDX_HOSTS:
        try:
            if not api.connect(host, 7709):
                continue
            bars = api.get_security_bars(9, market, pure, 0, 800)
            api.disconnect()
            if not bars:
                return None
            out = []
            for b in bars:
                dt = str(b["datetime"])[:10]
                dn = re.sub(r"\D", "", dt)
                if dn < sd or dn > ed:
                    continue
                out.append({
                    "date": dt, "open": float(b["open"]), "high": float(b["high"]),
                    "low": float(b["low"]), "close": float(b["close"]),
                    "volume": float(b.get("volume") or b.get("vol") or 0),
                    "amount": float(b.get("amount") or 0),
                    "volume_unit": "hands", "amount_unit": "yuan",
                    "adjustment": "none",
                    "_src": "pytdx"})
            return out or None
        except Exception:
            try:
                api.disconnect()
            except Exception:
                pass
            continue
    return None


# ---------------------------------------------------------------- 3) 腾讯财经（HTTP）
def _from_tencent(code, start, end, fq="qfq"):
    mkt, pure = _norm_code(code)
    pre = "sh" if mkt == "sh" else "sz"
    fqw = {"qfq": "qfq", "hfq": "hfq", "": "bfq"}[fq]
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
           f"{pre}{pure},day,,,320,{fqw}")  # 起止留空、只给数量，按本地日期过滤
    d = _curl_json(url)
    node = (d or {}).get("data", {}).get(pre + pure, {})
    key = fqw + "day" if fqw != "bfq" else "day"
    rows = node.get(key) or node.get("day") or []
    sd, ed = _norm_date(start), _norm_date(end)
    out = []
    for r in rows:
        dn = _norm_date(r[0])
        if dn < sd or dn > ed:
            continue
        out.append({"date": str(r[0]), "open": float(r[1]), "close": float(r[2]),
                    "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]),
                    "amount": None, "volume_unit": "hands", "amount_unit": "not_provided",
                    "adjustment": "none" if fqw == "bfq" else fqw, "_src": "tencent"})
    return out or None


# ---------------------------------------------------------------- 4) 新浪财经（HTTP）
def _from_sina(code, start, end, fq="qfq"):
    mkt, pure = _norm_code(code)
    pre = "sh" if mkt == "sh" else "sz"
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={pre}{pure}&scale=240&ma=no&datalen=320")
    out = subprocess.run(
        ["curl", "-s", "-4", "--compressed", "-m", "12", "-H", f"User-Agent: {UA}", url],
        capture_output=True, text=True, timeout=20)
    try:
        arr = json.loads(out.stdout)
    except Exception:
        return None
    sd, ed = _norm_date(start), _norm_date(end)
    res = []
    for r in arr:
        dn = _norm_date(r.get("day", ""))
        if dn < sd or dn > ed:
            continue
        res.append({"date": r["day"], "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r["volume"]), "amount": None,
                    "volume_unit": "shares", "amount_unit": "not_provided",
                    "adjustment": "none", "_src": "sina"})
    return res or None


# ---------------------------------------------------------------- 5) 东方财富 push2his（仅本机）
def _from_eastmoney(code, start, end, fq="qfq"):
    mkt, pure = _norm_code(code)
    secid = ("1." if mkt == "sh" else "0." if mkt == "sz" else "2.") + pure
    fqt = {"qfq": "1", "hfq": "2", "": "0"}[fq]
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
           f"&klt=101&fqt={fqt}&beg={_norm_date(start)}&end={_norm_date(end)}&ut={EM_UT}")
    d = _curl_json(url, timeout=8)
    rows = ((d or {}).get("data") or {}).get("klines") or []
    out = []
    for line in rows:
        p = line.split(",")
        if len(p) < 7:
            continue
        out.append({"date": p[0], "open": p[1], "close": p[2], "high": p[3],
                    "low": p[4], "volume": p[5], "amount": p[6],
                    "volume_unit": "hands", "amount_unit": "yuan",
                    "adjustment": {"0": "none", "1": "qfq", "2": "hfq"}.get(fqt, "unknown"),
                    "_src": "eastmoney"})
    return out or None


# ---------------------------------------------------------------- 6.5) 百度股市通 K线（独立域名，自带 MA）
def _from_baidu(code, start, end, fq="qfq"):
    import urllib.request as _u
    mkt, pure = _norm_code(code)
    pre = "sh" if mkt == "sh" else "sz"
    p = {"all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
         "isFutures": "false", "isStock": "true", "newFormat": "1",
         "group": "quotation_kline_ab", "finClientType": "pc",
         "code": pre + pure, "start_time": "", "ktype": "1"}
    qs = "&".join(f"{k}={v}" for k, v in p.items())
    req = _u.Request(
        "https://finance.pae.baidu.com/selfselect/getstockquotation?" + qs,
        headers={"User-Agent": "Mozilla/5.0",
                 "Accept": "application/vnd.finance-web.v1+json",
                 "Origin": "https://gushitong.baidu.com",
                 "Referer": "https://gushitong.baidu.com"})
    d = json.loads(_u.urlopen(req, timeout=8).read())
    md = (d.get("Result") or {}).get("newMarketData") or {}
    raw = (md.get("marketData") or "").split(";")
    # keys 顺序: [timestamp, time, open, close, volume, high, low, amount, range, ratio, ...]
    sd, ed = _norm_date(start), _norm_date(end)
    out = []
    for row in raw:
        cols = row.split(",")
        if len(cols) < 8:
            continue
        dn = _norm_date(cols[1])
        if dn < sd or dn > ed:
            continue
        out.append({"date": cols[1], "open": float(cols[2]), "close": float(cols[3]),
                    "high": float(cols[5]), "low": float(cols[6]),
                    "volume": float(cols[4]), "amount": float(cols[7]),
                    "volume_unit": "unknown", "amount_unit": "unknown",
                    "adjustment": "unknown", "_src": "baidu"})
    return out or None


# ---------------------------------------------------------------- 6) Tushare 中继（HTTP）
def _from_tushare_relay(code, start, end, fq="qfq"):  # noqa: E302
    if not TUSHARE_TOKEN:
        return None
    mkt, pure = _norm_code(code)
    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[mkt]
    ts_code = f"{pure}.{exchange}"
    # 注意：该中继的 daily 接口【不支持 adj 参数】，加上会返回 count=0，故始终不带 adj（返回非复权）。
    params = {"ts_code": ts_code, "start_date": _norm_date(start), "end_date": _norm_date(end)}
    rows = _tushare_query(
        "daily", params,
        fields="ts_code,trade_date,open,high,low,close,vol,amount",
        timeout=20,
    )
    if not rows:
        return None
    res = []
    for row in rows:
        # Relay daily returns vol in 百手 and amount in 千元.  Convert to
        # shares/yuan so every K-line adapter has the same unit contract.
        res.append({
            "date": row.get("trade_date"),
            "open": float(row.get("open") or 0),
            "high": float(row.get("high") or 0),
            "low": float(row.get("low") or 0),
            "close": float(row.get("close") or 0),
            "volume": float(row.get("vol") or 0) * 100,
            "amount": float(row.get("amount") or 0) * 1000,
            "volume_unit": "shares", "amount_unit": "yuan",
            "adjustment": "none",
            "_src": "tushare_relay"})
    return res or None


def _tushare_query(api_name, params=None, fields="", timeout=20):
    """Call the optional fast relay using the standard Tushare Pro envelope."""
    if not TUSHARE_TOKEN:
        return None
    try:
        from trade_system.tushare_relay import TushareRelayClient

        client = TushareRelayClient(
            token=TUSHARE_TOKEN, url=TUSHARE_RELAY, timeout=int(timeout),
            retries=1, min_interval_seconds=0.65,
        )
        rows = client.query_rows(api_name, params or {}, fields or "")
        return rows or None
    except Exception as exc:
        logger.warning("tushare relay query %s failed: %s", api_name, exc)
        return None



def _from_tushare_moneyflow(code, days=120):
    """Tushare moneyflow fallback, normalized to yuan and net-flow columns."""
    mkt, pure = _norm_code(code)
    ts_code = f"{pure}.{'SH' if mkt == 'sh' else 'SZ' if mkt == 'sz' else 'BJ'}"
    rows = _tushare_query(
        "moneyflow", {"ts_code": ts_code, "limit": int(days)},
        fields=("ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
                "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"),
    )
    if not rows:
        return None

    def num(row, key):
        try:
            return float(row.get(key)) * 10000
        except (TypeError, ValueError):
            return 0.0

    out = []
    for row in rows:
        small = num(row, "buy_sm_amount") - num(row, "sell_sm_amount")
        mid = num(row, "buy_md_amount") - num(row, "sell_md_amount")
        large = num(row, "buy_lg_amount") - num(row, "sell_lg_amount")
        super_net = num(row, "buy_elg_amount") - num(row, "sell_elg_amount")
        reported = row.get("net_mf_amount")
        try:
            total = float(reported) * 10000
        except (TypeError, ValueError):
            total = small + mid + large + super_net
        # TuShare ``net_mf_amount`` is the total net amount.  The project
        # contract calls main-order flow super-large + large; never put the
        # total into ``main_net``.
        main = super_net + large
        out.append({"date": str(row.get("trade_date") or "")[:10],
                    "main_net": main, "small_net": small, "mid_net": mid,
                    "large_net": large, "super_net": super_net,
                    "net_total": total, "_src": "tushare_relay",
                    "source_api": "moneyflow", "flow_definition": "main_orders_net",
                    "amount_unit": "yuan"})
    return out or None




_KLINE_SOURCES = [
    ("baostock", _from_baostock),
    ("pytdx", _from_pytdx),
    ("tencent", _from_tencent),
    ("sina", _from_sina),
    ("tushare_relay", _from_tushare_relay),
    ("baidu", _from_baidu),          # 独立域名、自带 MA，但实测偶发限流返回空，仅作末尾兜底
    ("eastmoney", _from_eastmoney),   # 沙箱内会被秒跳过（整族被封），本机自动生效
]


def get_kline(code, start="20260101", end="20500101", fq="qfq", timeout_per=10):
    """自动多源降级取日 K 线，返回统一结构 list[dict]。"""
    tried = []
    for name, fn in _KLINE_SOURCES:
        res = _run(lambda: fn(code, start, end, fq), timeout=timeout_per, label=name)
        if res:
            res.sort(key=lambda x: x["date"])
            return res
        tried.append(name)
    logger.warning("get_kline(%s): no source returned data (tried: %s)",
                   code, ", ".join(tried))
    return []


def _from_tushare_sector_flow(trade_date=None, limit=300):
    """Optional Tushare sector-flow mirror used after Eastmoney fails.

    The two Tushare endpoints do *not* share a unit or field contract:

    * ``moneyflow_ind_dc`` reports yuan and ``buy_*_amount`` fields are
      already net amounts (there is no corresponding ``sell_*`` field).
    * ``moneyflow_ind_ths`` reports the headline amounts in 亿元 and only
      exposes aggregate buy/sell values.

    Keep the conversion endpoint-specific so a provider fallback cannot make
    an otherwise plausible ranking 10,000x too large.
    """
    params = {"trade_date": _norm_date(trade_date)} if trade_date else {"limit": int(limit)}
    dc_fields = (
        "trade_date,content_type,ts_code,name,pct_change,close,net_amount,"
        "net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"
    )
    ths_fields = (
        "trade_date,ts_code,industry,name,pct_change,close,company_num,"
        "net_buy_amount,net_sell_amount,net_amount"
    )
    rows = None
    api_used = ""
    for api, fields in (("moneyflow_ind_dc", dc_fields), ("moneyflow_ind_ths", ths_fields)):
        rows = _tushare_query(api, params, fields=fields)
        if rows:
            api_used = api
            break
    if not rows:
        return None

    def pick(row, *keys):
        for key in keys:
            if row.get(key) not in (None, ""):
                return row.get(key)
        return None

    def number(value):
        try:
            return float(value) if value not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None

    def amount(row, *keys):
        value = number(pick(row, *keys))
        return value * 10000 if value is not None else None

    def yuan(value):
        value = number(value)
        return value if value is not None else None

    def ths_yuan(value):
        # THS sector endpoints document net buy/sell/net as 亿元.
        value = number(value)
        return value * 100000000 if value is not None else None

    out = []
    for row in rows:
        code = pick(row, "ts_code", "code", "industry_code", "index_code")
        if not code:
            continue
        if api_used == "moneyflow_ind_dc":
            record = {
                "sector_code": str(code), "sector_name": pick(row, "name", "industry_name"),
                "sector_type": pick(row, "content_type") or "unknown",
                "change_pct": number(pick(row, "pct_change", "change_pct")),
                "main_net": yuan(pick(row, "net_amount", "main_net")),
                "super_net": yuan(pick(row, "buy_elg_amount")),
                "large_net": yuan(pick(row, "buy_lg_amount")),
                "mid_net": yuan(pick(row, "buy_md_amount")),
                "small_net": yuan(pick(row, "buy_sm_amount")),
                "amount_unit": "yuan",
            }
        else:
            record = {
                "sector_code": str(code), "sector_name": pick(row, "name", "industry_name"),
                "sector_type": "ths_industry",
                "change_pct": number(pick(row, "pct_change", "change_pct")),
                "main_net": ths_yuan(pick(row, "net_amount", "main_net")),
                "super_net": None, "large_net": None, "mid_net": None, "small_net": None,
                "amount_unit": "yuan_from_100m_yuan",
            }
        record.update({"_src": "tushare_relay", "source_api": api_used, "raw": row})
        out.append(record)
    return out or None
