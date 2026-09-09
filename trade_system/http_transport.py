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
