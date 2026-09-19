"""K-line source adapters with multi-source fallback.

Callers import these provider implementations directly.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
import urllib.request

from trade_system.logging_setup import get_logger
from trade_system.http_transport import read_verified_once, request_budget, request_deadline
from trade_system.units import requested_adjustment

try:
    from trade_system.config import SETTINGS as _PROJECT_SETTINGS
except Exception:
    _PROJECT_SETTINGS = {}

logger = get_logger(__name__)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
# Tushare is an optional last-resort source.  Keep credentials out of source
# control; the non-Tushare providers remain fully usable when these are empty.
XIAODEFA_URL = _PROJECT_SETTINGS.get("XIAODEFA_URL") or "https://t.xiaodefa.top/"
XIAODEFA_TOKEN = _PROJECT_SETTINGS.get("XIAODEFA_TOKEN") or _PROJECT_SETTINGS.get("TUSHARE_XIAODEFA_TOKEN") or ""

# pytdx / mootdx 可达的交易所行情服务器（沙箱实测 TCP 7709 通，2026-07），合并两份清单自动轮询
TDX_HOSTS = [
    "115.238.90.165", "180.153.18.170", "218.108.98.36", "60.12.138.11",
    # 来自 a-stock-data 仓库实测可用清单（2026-06 验证，沙箱可达）
    "119.97.185.59", "124.70.133.119", "116.205.183.150", "123.60.73.44",
    "116.205.163.254", "121.36.225.169", "123.60.70.228", "124.71.9.153",
    "110.41.147.114", "124.71.187.122",
]


# ---------------------------------------------------------------- 工具
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


def _read_json(url, timeout=12):
    raw = read_verified_once(urllib.request.Request(url, headers={"User-Agent": UA}),
                             timeout=timeout, max_bytes=8_000_000)
    return json.loads(_auto_decode(raw))


@request_budget(10)
def _sdk_call(operation, *args):
    """One owned SDK child; timeout reaps it before releasing acquisition."""
    command = [sys.executable, '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);from trade_system.adapters.kline_sources import _sdk_worker;_sdk_worker()',
        str(Path(__file__).resolve().parents[2])]
    timeout = request_deadline.get() - time.monotonic()
    if timeout <= 0:
        raise TimeoutError('SDK deadline exhausted')
    try:
        result = subprocess.run(command, input=json.dumps([operation, args]).encode('utf-8'),
            capture_output=True, timeout=timeout,
            creationflags=0x08000000 if sys.platform == 'win32' else 0)
    except subprocess.TimeoutExpired:
        raise TimeoutError('SDK deadline exhausted') from None
    if result.returncode or len(result.stdout) > 8_000_000:
        raise RuntimeError('SDK worker failed or response budget exceeded')
    return json.loads(result.stdout)


def _sdk_worker():
    import os
    from contextlib import redirect_stdout, redirect_stderr
    operation, args = json.loads(sys.stdin.buffer.read(4096))
    readers = {'baostock': _baostock_rows, 'pytdx': _pytdx_rows, 'pytdx_minutes': _pytdx_minutes_rows}
    # SDK diagnostics never enter the result protocol or grow the pipe buffer.
    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
        rows = readers[operation](*args)
    raw = json.dumps(rows, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw) > 8_000_000:
        raise ValueError('SDK response budget exceeded')
    sys.stdout.buffer.write(raw)


def _from_baostock(code, start, end, fq="qfq"):
    if requested_adjustment(fq) != 'none':
        return None
    return _sdk_call('baostock', code, start, end, fq)


def _from_pytdx(code, start, end, fq="qfq"):
    if requested_adjustment(fq) != 'none':
        return None
    return _sdk_call('pytdx', code, start, end, fq)


def _from_pytdx_minutes(code, date=None):
    return _sdk_call('pytdx_minutes', code, date)


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



# ---------------------------------------------------------------- 1) baostock
def _baostock_rows(code, start, end, fq="qfq"):
    if requested_adjustment(fq) != 'none':
        return None
    import baostock as bs
    mkt, pure = _norm_code(code)
    adj = '3'
    try:
        login = bs.login()
        if getattr(login, 'error_code', '1') != '0':
            return None
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
    finally:
        bs.logout()



# ---------------------------------------------------------------- 2) pytdx（交易所 TCP 直连）
def _pytdx_rows(code, start, end, fq="qfq"):
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
    d = _read_json(url)
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
    arr = _read_json(url) or []
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
    d = _read_json(url, timeout=8)
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
    d = json.loads(read_verified_once(req, timeout=8, max_bytes=8_000_000))
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
def _from_xiaodefa(code, start, end, fq="qfq"):  # noqa: E302
    if not XIAODEFA_TOKEN:
        return None
    mkt, pure = _norm_code(code)
    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[mkt]
    ts_code = f"{pure}.{exchange}"
    # 注意：该中继的 daily 接口【不支持 adj 参数】，加上会返回 count=0，故始终不带 adj（返回非复权）。
    params = {"ts_code": ts_code, "start_date": _norm_date(start), "end_date": _norm_date(end)}
    rows = _xiaodefa_query(
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
            "_src": "xiaodefa"})
    return res or None


def _xiaodefa_query(api_name, params=None, fields="", timeout=20):
    from trade_system.xiaodefa_source import XiaodefaClient
    client = XiaodefaClient(token=XIAODEFA_TOKEN, url=XIAODEFA_URL, timeout=timeout, max_retries=1)
    return client.query_rows(api_name, params or {}, fields)



def _from_xiaodefa_moneyflow(code, days=120):
    """Tushare moneyflow fallback, normalized to yuan and net-flow columns."""
    mkt, pure = _norm_code(code)
    ts_code = f"{pure}.{'SH' if mkt == 'sh' else 'SZ' if mkt == 'sz' else 'BJ'}"
    rows = _xiaodefa_query(
        "moneyflow", {"ts_code": ts_code, "limit": int(days)},
        fields=("ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
                "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"),
    )
    if not rows:
        return None

    from trade_system.flow_contract import normalize_stock_flow_row
    return [{**normalize_stock_flow_row({**row, "source_api": "moneyflow",
                "amount_unit": "10000_yuan"}, "xiaodefa"),
             "date": str(row.get("trade_date") or "")[:10],
             "_src": "xiaodefa", "raw": row} for row in rows]



def get_kline(code, start=None, end=None, fq="qfq", timeout_per=10):
    """Thin compatibility call into the sole acquisition owner."""
    from trade_system.resilient_sources import get
    if requested_adjustment(fq) == 'unknown':
        raise ValueError('unsupported requested adjustment')
    params = {"fq": fq}
    if start is not None: params["start"] = start
    if end is not None: params["end"] = end
    rows, meta = get('kline', code, timeout_per=timeout_per, **params)
    return sorted(rows, key=lambda row: row['date']) if rows and meta['status'] in {'live','fresh','refreshed'} else []


def _from_xiaodefa_sector_flow(trade_date=None, limit=300):
    """Read DC sector flow in yuan; an empty DC result cannot become THS flow."""
    params = {"trade_date": _norm_date(trade_date)} if trade_date else {"limit": int(limit)}
    fields = (
        "trade_date,content_type,ts_code,name,pct_change,close,net_amount,"
        "net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"
    )
    rows = _xiaodefa_query("moneyflow_ind_dc", params, fields=fields)
    if not rows:
        return None

    def number(value):
        try:
            return float(value) if value not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None

    return [{
        "sector_code": str(row["ts_code"]), "sector_name": row.get("name"),
        "sector_type": row.get("content_type") or "unknown",
        "change_pct": number(row.get("pct_change")),
        "main_net": number(row.get("net_amount")),
        "super_net": number(row.get("buy_elg_amount")),
        "large_net": number(row.get("buy_lg_amount")),
        "mid_net": number(row.get("buy_md_amount")),
        "small_net": number(row.get("buy_sm_amount")),
        "amount_unit": "yuan", "_src": "xiaodefa",
        "source_api": "moneyflow_ind_dc", "raw": row,
    } for row in rows if row.get("ts_code")] or None


import datetime


def _from_xiaodefa_basic(list_status="L"):
    """全量股票列表（ts_code/name/industry/market/list_date），Tushare 中继，用于本地参考镜像（总量容灾）。"""
    return _xiaodefa_query(
        "stock_basic", {"list_status": list_status},
        fields="ts_code,symbol,name,industry,market,list_date",
    )

def _pytdx_minutes_rows(code, date=None):
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
