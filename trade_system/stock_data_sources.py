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
import subprocess, json, socket, re, concurrent.futures, os, threading, atexit
import urllib.request as _u, urllib.parse as _up, urllib.error as _ue, hashlib, io  # noqa: E402

from trade_system.eastmoney_clist_guard import (
    DELAY_CLIST_GUARD,
    DEFAULT_CLIST_GUARD,
    EastmoneyClistUnavailable,
)

try:
    from config import SETTINGS as _PROJECT_SETTINGS
except Exception:
    _PROJECT_SETTINGS = {}

# 防止任何源在沙箱内卡死：全局 socket 超时
socket.setdefaulttimeout(10)

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
def _run(func, timeout=10):
    """在线程里跑源，超时/异常都返回 None，绝不阻塞主流程。"""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = executor.submit(func)
        try:
            return fut.result(timeout=timeout)
        except Exception:
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
    except Exception:
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
                    "volume": float(r[5] or 0), "amount": float(r[6] or 0), "_src": "baostock"})
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
                    "amount": 0.0, "_src": "tencent"})
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
                    "volume": float(r["volume"]), "amount": 0.0, "_src": "sina"})
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
                    "low": p[4], "volume": p[5], "amount": p[6], "_src": "eastmoney"})
    return out or None


# ---------------------------------------------------------------- 6.5) 百度股市通 K线（独立域名，自带 MA）
def _from_baidu(code, start, end, fq="qfq"):
    import urllib.request as _u, urllib.parse as _up
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
    keys = md.get("keys") or []
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
                    "volume": float(cols[4]), "amount": float(cols[7]), "_src": "baidu"})
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
    except Exception:
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


# ---------------------------------------------------------------- 统一入口：K 线
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
    for name, fn in _KLINE_SOURCES:
        res = _run(lambda: fn(code, start, end, fq), timeout=timeout_per)
        if res:
            res.sort(key=lambda x: x["date"])
            return res
    return []


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
    import urllib.request as _u, urllib.parse as _up
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
                   sort_columns="", sort_types="-1", columns="ALL", timeout=15):
    """东财数据中心统一查询（龙虎榜/解禁/融资融券/大宗/股东/分红/研报共用）。
    columns=ALL 解锁全部字段（沙箱实测必须带，否则只回部分列）。"""
    params = {"reportName": report_name, "columns": columns, "filter": filter_str,
              "pageNumber": "1", "pageSize": str(page_size),
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
    stocks = []
    for row in data:
        net = (row.get("BILLBOARD_NET_AMT") or 0) / 1e4
        stocks.append({"code": row.get("SECURITY_CODE", ""), "name": row.get("SECURITY_NAME_ABBR", ""),
                       "reason": row.get("EXPLANATION", ""), "close": row.get("CLOSE_PRICE") or 0,
                       "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
                       "net_buy_wan": round(net, 1),
                       "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 1e4, 1),
                       "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 1e4, 1),
                       "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2)})
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
        "fields": "f2,f3,f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87,f104,f105,f128,f136,f140",
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
        "fields": "f2,f3,f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87,f104,f105,f128,f136,f140",
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


def _f(d, k):
    try:
        v = d.get(k)
        return float(v) if v not in (None, "", "-") else None
    except Exception:
        return None


def _d(date_fmt="%Y-%m-%d"):
    return __import__("datetime").date.today().strftime(date_fmt)


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
