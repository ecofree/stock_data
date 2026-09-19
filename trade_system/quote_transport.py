"""One bounded Tencent transport shared by legacy and research observations."""
import re
import time
from urllib.request import Request

MAX_CODES=200
BATCH_SIZE=50
MAX_BYTES=1_000_000


def canonical_codes(codes):
    result=[]
    for value in codes:
        match=re.fullmatch(r'(?:(sh|sz|bj))?(\d{6})(?:\.(SH|SZ|BJ))?',str(value),re.I)
        if not match:raise ValueError('six-digit exchange-qualified quote code required')
        prefix,code,suffix=match.groups()
        expected=market_prefix(code)
        if (prefix and prefix.lower()!=expected) or (suffix and suffix.lower()!=expected):
            raise ValueError('quote exchange/code mismatch')
        result.append(code)
    result=sorted(set(result))
    if len(result)>MAX_CODES:raise ValueError('quote universe exceeds 200')
    return result


def batches(codes):
    codes=canonical_codes(codes)
    return [codes[i:i+BATCH_SIZE] for i in range(0,len(codes),BATCH_SIZE)]


def market_prefix(code):
    return 'bj' if code.startswith(('4','8','92')) else 'sh' if code.startswith(('5','6','9')) else 'sz'


def request_bytes(codes, *, timeout=12):
    """One verified HTTPS attempt, no transport fallback or retries."""
    from trade_system.http_transport import read_verified_once
    codes=canonical_codes(codes)
    if not 1<=len(codes)<=BATCH_SIZE:raise ValueError('bounded nonempty quote batch required')
    symbols=[market_prefix(c)+c for c in codes]
    request=Request('https://qt.gtimg.cn/q='+','.join(symbols),headers={'User-Agent':'Mozilla/5.0'})
    return read_verified_once(request, timeout=timeout, max_bytes=MAX_BYTES)


def parse_parts(raw,codes):
    """Retain duplicate/mismatched identity as a failure, not last-row-wins."""
    if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:raise ValueError('bounded quote bytes required')
    requested=set(canonical_codes(codes));result={}
    for line in raw.decode('gb18030').split(';'):
        if not line.strip():continue
        match=re.fullmatch(r'\s*v_((?:sh|sz|bj)\d{6})="([^"\r\n]*)"\s*',line)
        if not match:raise ValueError('unknown quote response envelope')
        symbol,payload=match.groups();code=canonical_codes([symbol])[0]
        if code not in requested or code in result:raise ValueError('unexpected or duplicate quote identity')
        parts=payload.split('~')
        if len(parts)<33 or parts[2]!=code:raise ValueError('quote payload identity/width differs')
        result[code]=parts
    return result


def fetch_parts(codes):
    result={}
    deadline = time.monotonic() + 12
    for batch in batches(codes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('quote batch deadline exhausted')
        result.update(parse_parts(request_bytes(batch, timeout=remaining), batch))
    return result




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
    d = fetch_parts([code])
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

    result = {
        "code": parts[2],
        "name": parts[1],
        "price": g("price"), "pre_close": g("pre_close"), "open": g("open"),
        "high": g("high"), "low": g("low"), "volume": g("volume"),
        "amount": g("amount_wan"), "change": g("change"), "change_pct": g("change_pct"),
        "turnover": g("turnover"), "pe_ttm": g("pe_ttm"), "pb": g("pb"),
        "total_mv": g("total_mv_yi"), "circ_mv": g("circ_mv_yi"),
        "total_mv_unit": "100m_yuan", "circ_mv_unit": "100m_yuan",
        "limit_up": g("limit_up"), "limit_down": g("limit_down"),
        "time": parts[_TENCENT_Q["time"]], "_src": "tencent", "raw": parts,
    }
    from trade_system.units import market_caps
    result.update(market_caps(result))
    return result

def _from_tencent_bid_ask(code):
    """五档盘口（腾讯 qt.gtimg.cn）：买一~五 / 卖一~五 价格与量，零依赖、可靠。"""
    d = fetch_parts([code])
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
