"""One bounded Tencent transport shared by legacy and research observations."""
import re
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


def request_bytes(codes):
    """One verified HTTPS attempt, no transport fallback or retries."""
    from trade_system.http_transport import open_verified_once
    codes=canonical_codes(codes)
    if not 1<=len(codes)<=BATCH_SIZE:raise ValueError('bounded nonempty quote batch required')
    symbols=[market_prefix(c)+c for c in codes]
    request=Request('https://qt.gtimg.cn/q='+','.join(symbols),headers={'User-Agent':'Mozilla/5.0'})
    with open_verified_once(request,timeout=12) as response:raw=response.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('quote response byte budget exceeded')
    return raw


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
    for batch in batches(codes):result.update(parse_parts(request_bytes(batch),batch))
    return result
