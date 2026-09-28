"""Shared HTTPS transport policy for external data providers.

The scheduled SYSTEM account does not always use the same certificate store as
the interactive Python process. Keep verification enabled, prefer the Windows
trust store when available, and allow an operator-installed CA bundle for an
inspected/proxied network.
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from contextvars import ContextVar
from contextlib import contextmanager

request_deadline = ContextVar('request_deadline', default=None)
diagnostic_state = ContextVar('diagnostic_state', default=None)

from trade_system.config import SETTINGS


@contextmanager
def diagnostic_budget():
    """One explicit diagnosis: at most six wire attempts, two per endpoint.

    A nested consumer shares its parent's counters and stop state. No proxy or
    provider policy is changed; callers must establish entitlement first.
    """
    parent = diagnostic_state.get()
    state = parent if parent is not None else {'attempts': 0, 'endpoints': {}, 'stopped': None}
    token = diagnostic_state.set(state)
    try:
        yield state
    finally:
        diagnostic_state.reset(token)


def _diagnostic_attempt(request):
    from urllib.parse import urlsplit
    state = diagnostic_state.get()
    if state is None:
        return
    url = urlsplit(request.full_url)
    endpoint = (url.hostname, url.path)  # Never retain query credentials.
    if state['stopped']:
        raise RuntimeError('diagnostic stopped: ' + state['stopped'])
    if state['attempts'] >= 6 or state['endpoints'].get(endpoint, 0) >= 2:
        state['stopped'] = 'request_budget_exhausted'
        raise RuntimeError('diagnostic request budget exhausted')
    state['attempts'] += 1
    state['endpoints'][endpoint] = state['endpoints'].get(endpoint, 0) + 1


def stop_diagnostic(reason):
    """Business-level auth/permission/rate/cooldown responses stop the context."""
    if reason not in {'authentication_failed', 'permission_denied', 'rate_limited', 'cooldown', 'business_rejected'}:
        raise ValueError('explicit diagnostic stop category required')
    if diagnostic_state.get() is not None:
        diagnostic_state.get()['stopped'] = reason


def inherited_request_remaining():
    import math
    import time
    value = os.environ.get('STOCKDATA_REQUEST_DEADLINE_EPOCH')
    if value is None:
        return float('inf')
    remaining = float(value)-time.time()
    if not math.isfinite(remaining):
        raise ValueError('invalid inherited request deadline')
    return remaining


@contextmanager
def request_budget(seconds):
    """Share one wall-clock budget across waiting, fallbacks and pages."""
    import math
    import time
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('positive finite request budget required')
    deadline = time.monotonic() + min(seconds, inherited_request_remaining())
    if request_deadline.get() is not None:
        deadline = min(deadline, request_deadline.get())
    if deadline <= time.monotonic():
        raise TimeoutError('request deadline exhausted')
    token = request_deadline.set(deadline)
    try:
        yield deadline
    finally:
        request_deadline.reset(token)



request_observer = ContextVar('request_observer', default=None)


@contextmanager
def measure_requests():
    """Observe the shared real transport in this context, without initiating I/O."""
    records=[];token=request_observer.set(records)
    try:yield records
    finally:request_observer.reset(token)


@contextmanager
def request_receipt(request):
    """Record bounded fingerprints, never URL arguments, bodies or credentials."""
    import hashlib
    import json
    import uuid
    from datetime import datetime,timezone
    from urllib.parse import urlsplit
    from trade_system.logging_setup import get_logger
    body=request.data or b''
    try:body=json.dumps(json.loads(body),sort_keys=True,separators=(',',':')).encode()
    except (ValueError,UnicodeError):pass
    key=hashlib.sha256(request.get_method().encode()+b'\0'+request.full_url.encode()+b'\0'+body+
        json.dumps(sorted((k.lower(),v) for k,v in request.header_items())).encode()).hexdigest()
    value={'attempt_id':uuid.uuid4().hex,'request_key':key,'host':urlsplit(request.full_url).hostname,
           'started_at':datetime.now(timezone.utc).isoformat(),'status':'outcome_unknown'}
    # Scheduler-supplied attribution is constrained to non-secret identifiers.
    # Standalone/SDK requests without this context remain explicitly unmeasured.
    import re
    try:context=json.loads(os.environ.get('STOCKDATA_REQUEST_CONTEXT','{}'))
    except ValueError:context={}
    allowed=('demand_id','consumer','refresh_reason','phase','session','revision_of','coverage_before',
             'product_id','semantic_version','scope')
    value['attribution']={key:item for key,item in context.items() if key in allowed
        and isinstance(item,str) and re.fullmatch(r'[A-Za-z0-9_.:/ -]{1,160}',item)} if isinstance(context,dict) else {}
    logger=get_logger('http_transport')
    logger.info('request_receipt %s',json.dumps(dict(value,event='started'),sort_keys=True))
    try:yield value
    except Exception as exc:
        value['error_type']=type(exc).__name__
        value.setdefault('error_category',classify_transport_error(exc))
        if isinstance(exc,urllib.error.HTTPError):value.update(status='http_error',http_status=exc.code)
        raise
    finally:
        value['finished_at']=datetime.now(timezone.utc).isoformat()
        logger.info('request_receipt %s',json.dumps(dict(value,event='finished'),sort_keys=True))
        if request_observer.get() is not None:request_observer.get().append(value)


def summarize_requests(records):
    """Same response bytes are repeat information, not proof of needless retries."""
    attempts={};conflicts=set()
    for item in records:
        key=item['attempt_id'];previous=attempts.get(key)
        if previous and previous.get('event')=='finished' and item.get('event')=='finished' and previous!=item:conflicts.add(key)
        if previous is None or item.get('event')!='started':attempts[key]=item
    seen=set();repeat=0
    for r in sorted(attempts.values(),key=lambda v:v['started_at']):
        if r.get('status')=='response_received':
            key=(r['request_key'],r['response_sha256'])
            repeat+=key in seen;seen.add(key)
    reasons={}
    products={}
    for row in attempts.values():
        reason=row.get('attribution',{}).get('refresh_reason','unattributed')
        reasons[reason]=reasons.get(reason,0)+1
        context=row.get('attribution',{})
        product=context.get('product_id') or context.get('demand_id') or 'unattributed'
        item=products.setdefault(product,{'transport_attempts':0,'responses_received':0,'errors':0,
            'first_received_at':None,'last_received_at':None,'consumers':set()})
        item['transport_attempts']+=1
        item['responses_received']+=row['status']=='response_received'
        item['errors']+=bool(row.get('error_category') or row.get('http_status'))
        if context.get('consumer'):item['consumers'].add(context['consumer'])
        if row['status']=='response_received' and row.get('finished_at'):
            received=row['finished_at']
            item['first_received_at']=min(item['first_received_at'] or received,received)
            item['last_received_at']=max(item['last_received_at'] or received,received)
    for item in products.values():item['consumers']=sorted(item['consumers'])
    return {'transport_attempts':len(attempts),'responses_received':sum(r['status']=='response_received' for r in attempts.values()),
        'same_request_same_response':repeat,'unknown_outcomes':sum(r['status']=='outcome_unknown' for r in attempts.values()),
        'conflicting_receipts':len(conflicts),'scope':'observed_shared_transport_only_not_all_providers_or_avoidable_cost',
        'attempts_by_refresh_reason':reasons,
        'products':products,
        'attributed_attempts':sum(bool(r.get('attribution',{}).get('demand_id')) for r in attempts.values()),
        'avoidable_duplicates':None,'missing':['verified_coverage_and_revision_policy_for_cost_attribution']}


def request_metrics(log_paths):
    """Read existing request logs only; absent instrumentation stays unmeasured."""
    import json
    import hashlib
    records=[];sources=[]
    if not 1<=len(log_paths)<=30:raise ValueError('one to thirty explicit logs required')
    for name in log_paths:
        path=Path(name).resolve(strict=True)
        if path.stat().st_size>100_000_000:raise ValueError('request log exceeds read budget')
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for line in stream:
                digest.update(line)
                if b'request_receipt ' not in line:continue
                value=json.loads(line.split(b'request_receipt ',1)[1])
                if value.get('event') not in ('started','finished'):raise ValueError('unknown request receipt event')
                records.append(value)
                if len(records)>100000:raise ValueError('request receipt budget exceeded')
        sources.append({'name':path.name,'sha256':digest.hexdigest()})
    return dict(summarize_requests(records),sources=sources,
                status='observed_log_receipts' if records else 'unmeasured_no_request_receipts')


def _setting(name: str, default: str = "") -> str:
    return str(SETTINGS.get(name) or os.environ.get(name) or default).strip()


def _use_system_store() -> bool:
    raw = _setting("KPL_SSL_USE_SYSTEM_STORE", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def _direct_first() -> bool:
    raw = _setting("KPL_DIRECT_FIRST", "1").lower()
    return raw not in {"0", "false", "no", "off"}


@lru_cache(maxsize=1)
def default_ssl_context() -> ssl.SSLContext:
    """Build the verified context used by KPL/HiThink HTTP calls."""
    ca_bundle = _setting("KPL_SSL_CA_BUNDLE")
    if ca_bundle:
        path = Path(ca_bundle).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"KPL_SSL_CA_BUNDLE does not exist: {path}")
        return ssl.create_default_context(cafile=str(path))

    if _use_system_store():
        try:
            import truststore

            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            pass
    return ssl.create_default_context()


def ssl_context_note() -> str:
    """Return a non-secret diagnostic for reports and transport errors."""
    bundle = _setting("KPL_SSL_CA_BUNDLE")
    direct = "direct_first" if _direct_first() else "proxy_first"
    if bundle:
        return f"configured_ca_bundle={Path(bundle).expanduser()} {direct}"
    if _use_system_store():
        try:
            import truststore  # noqa: F401

            return f"windows_system_truststore {direct}"
        except ImportError:
            return f"python_default_store_truststore_unavailable {direct}"
    return f"python_default_store {direct}"


def classify_transport_error(exc: BaseException) -> str:
    """Return a stable, non-secret code for a verified transport failure.

    ``urllib`` wraps TLS failures in ``URLError`` and the useful certificate
    exception is often stored in ``exc.reason``.  Keeping this classification
    here prevents the production client and the audit scripts from reporting
    the same failure with different generic labels.
    """
    import http.client
    import socket
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f'http_{exc.code}'
    if isinstance(reason, socket.gaierror):
        return 'dns_resolution_failed'
    if isinstance(reason, (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError)):
        return 'connection_interrupted'
    text = f"{exc} {reason}".upper()
    if isinstance(reason, ssl.SSLCertVerificationError) or any(
        marker in text
        for marker in (
            "CERTIFICATE_VERIFY_FAILED",
            "CERTIFICATE_VERIFY_ERROR",
            "SELF SIGNED CERTIFICATE",
            "CERTIFICATE UNKNOWN",
        )
    ):
        return "tls_certificate_untrusted"
    if isinstance(reason, TimeoutError) or "TIMED OUT" in text or "DEADLINE" in text:
        return "network_timeout"
    if "NAME OR SERVICE NOT KNOWN" in text or "GETADDRINFO FAILED" in text:
        return "dns_resolution_failed"
    if "CONNECTIONREFUSED" in text or "REFUSED" in text or "10061" in text:
        return "connection_refused"
    if "RESET" in text or "10054" in text or "REMOTEDISCONNECTED" in text:
        return "connection_interrupted"
    return "network_error"


@lru_cache(maxsize=2)
def _verified_opener(direct: bool) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if direct:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler(context=default_ssl_context()))
    return urllib.request.build_opener(*handlers)


def open_verified(request: urllib.request.Request, *, timeout: float):
    """Open a provider request with verification and transport fallback.

    A direct attempt is useful on hosts whose local proxy re-signs HTTPS with
    an untrusted root.  If direct networking is unavailable, retry through
    the environment-configured proxy.  HTTP responses are never retried
    across transports, and neither path permits certificate bypass.
    """
    if os.environ.get('STOCKDATA_REQUEST_DEADLINE_EPOCH') is not None or diagnostic_state.get() is not None:
        # Scheduled phases select one verified route and use a bounded network
        # child. A DNS/body stall must not strand the database writer.
        import io
        return io.BytesIO(read_verified_once(request, timeout=min(60, timeout), max_bytes=8_000_000))
    if not _direct_first():
        return _verified_opener(False).open(request, timeout=timeout)
    try:
        return _verified_opener(True).open(request, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError:
        return _verified_opener(False).open(request, timeout=timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API credentials must never follow an unapproved redirect destination.
        raise urllib.error.HTTPError(req.full_url,code,'provider redirect refused',headers,fp)


def open_verified_once(request: urllib.request.Request, *, timeout: float):
    """One selected transport, no redirect or fallback. Timeout is socket-level."""
    handlers=[_NoRedirect(),urllib.request.HTTPSHandler(context=default_ssl_context())]
    if _direct_first():
        handlers.append(urllib.request.ProxyHandler({}))
    _diagnostic_attempt(request)
    try:
        return urllib.request.build_opener(*handlers).open(request,timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            stop_diagnostic({401: 'authentication_failed', 403: 'permission_denied', 429: 'rate_limited'}[exc.code])
        raise


def _read_verified_response(request, timeout, max_bytes):
    with open_verified_once(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('response byte budget exceeded')
    return raw


def _response_worker():
    """Private one-request child: the parent can terminate blocked DNS/body reads."""
    import json
    import sys
    data = json.loads(sys.stdin.buffer.read(20_000_000))
    request = urllib.request.Request(data['url'],
        data=data['body'].encode('utf-8') if data['body'] is not None else None,
        headers=data['headers'], method=data['method'])
    raw = b''
    try:
        raw = _read_verified_response(request, data['timeout'], data['max_bytes'])
        status = {'ok': True}
    except urllib.error.HTTPError as exc:
        status = {'http_status': exc.code}
    except ValueError:
        status = {'error': 'response byte budget exceeded'}
    except Exception as exc:
        cause = getattr(exc, 'reason', exc)
        status = {'error': classify_transport_error(exc),
                  'cause_type': type(cause).__name__,
                  'os_error': getattr(cause, 'winerror', None) or getattr(cause, 'errno', None)}
    sys.stdout.buffer.write(json.dumps(status).encode('ascii') + b'\n' + raw)


def read_verified_once(request, *, timeout, max_bytes):
    """One verified request, with a wall-clock limit including DNS and body reads."""
    import json
    import subprocess
    import sys
    import math
    import time
    if not math.isfinite(timeout) or timeout > 60:
        raise ValueError("transport timeout must be finite and at most 60 seconds")
    if request_deadline.get() is not None:
        timeout = min(timeout, request_deadline.get()-time.monotonic())
    timeout = min(timeout, inherited_request_remaining())
    if timeout <= 0:
        raise TimeoutError('transport deadline exhausted')
    if not 1 <= max_bytes <= 8_000_000:
        raise ValueError('bounded response size required')
    payload = json.dumps({'url': request.full_url, 'headers': dict(request.header_items()),
        'method': request.get_method(), 'body': request.data.decode('utf-8') if request.data else None,
        'timeout': timeout, 'max_bytes': max_bytes}).encode('utf-8')
    if len(payload) > 20_000_000:
        raise ValueError('request byte budget exceeded')
    # Credentials travel through stdin, never command-line arguments or diagnostics.
    command = [sys.executable, '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);from trade_system.http_transport import _response_worker;_response_worker()',
        str(Path(__file__).resolve().parents[1])]
    _diagnostic_attempt(request)
    with request_receipt(request) as receipt:
        try:
            result = subprocess.run(command, input=payload, capture_output=True, timeout=timeout,
                                    creationflags=0x08000000 if sys.platform == 'win32' else 0)
        except subprocess.TimeoutExpired:
            # run() kills and waits for its one child before raising; no background reader survives.
            raise TimeoutError('transport deadline exhausted') from None
        if result.returncode:
            raise urllib.error.URLError('verified transport worker failed')
        header, separator, raw = result.stdout.partition(b'\n')
        try:
            status = json.loads(header)
        except ValueError:
            raise urllib.error.URLError('invalid transport worker response') from None
        if not separator:
            raise urllib.error.URLError('incomplete transport worker response')
        if 'http_status' in status:
            if status['http_status'] in (401, 403, 429):
                stop_diagnostic({401: 'authentication_failed', 403: 'permission_denied', 429: 'rate_limited'}[status['http_status']])
            raise urllib.error.HTTPError(request.full_url, status['http_status'], 'request failed', None, None)
        if status.get('error') == 'response byte budget exceeded' or len(raw) > max_bytes:
            raise ValueError('response byte budget exceeded')
        if status.get('ok') is not True:
            category = status.get('error')
            if category not in {'tls_certificate_untrusted', 'network_timeout', 'dns_resolution_failed',
                                'connection_refused', 'connection_interrupted', 'network_error'}:
                category = 'network_error'
            receipt['error_category'] = category
            cause_type = status.get('cause_type')
            if isinstance(cause_type, str) and cause_type.isidentifier() and len(cause_type) <= 80:
                receipt['cause_type'] = cause_type
            if isinstance(status.get('os_error'), int):
                receipt['os_error'] = status['os_error']
            raise urllib.error.URLError(category)
        import hashlib
        receipt.update(status='response_received',response_sha256=hashlib.sha256(raw).hexdigest(),response_bytes=len(raw))
        return raw


def read_public_pdf(url):
    """Bounded transport for validated public-document plans; no credentials."""
    raw = read_verified_once(urllib.request.Request(url, headers={'User-Agent': 'stock-data-evidence/1'}),
                             timeout=15, max_bytes=4*1024*1024)
    if not raw.startswith(b'%PDF-'):
        raise ValueError('response is not a PDF')
    return raw
