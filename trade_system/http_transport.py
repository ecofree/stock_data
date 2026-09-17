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

request_deadline = ContextVar('request_deadline', default=None)

from trade_system.config import SETTINGS


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
    reason = getattr(exc, "reason", exc)
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
    if isinstance(reason, TimeoutError) or "TIMED OUT" in text:
        return "network_timeout"
    if "NAME OR SERVICE NOT KNOWN" in text or "GETADDRINFO FAILED" in text:
        return "dns_resolution_failed"
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
    return urllib.request.build_opener(*handlers).open(request,timeout=timeout)


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
    except Exception:
        status = {'error': 'verified transport failed'}
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
        raise urllib.error.HTTPError(request.full_url, status['http_status'], 'request failed', None, None)
    if status.get('error') == 'response byte budget exceeded' or len(raw) > max_bytes:
        raise ValueError('response byte budget exceeded')
    if status.get('ok') is not True:
        raise urllib.error.URLError('verified transport failed')
    return raw
