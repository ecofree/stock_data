"""Single bounded HTTP client for the retained xiaodefa TuShare channel."""
from __future__ import annotations
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from trade_system.config import SETTINGS
DEFAULT_XIAODEFA_URL = "https://t.xiaodefa.top/"
DEFAULT_PAGE_SIZE = 4000
MAX_ROWS_SAFETY = 40000


class XiaodefaError(RuntimeError):
    """Raised when the xiaodefa relay cannot serve a request."""


def _settings_value(name: str, default: str = "") -> str:
    return str(SETTINGS.get(name, "") or "").strip() or default


class XiaodefaClient:
    """One verified transport and retry budget for the retained TuShare channel."""

    def __init__(self, token=None, url=None, *, min_interval_seconds=0.65,
                 max_retries=3, timeout=20, max_response_bytes=8_000_000,
                 runner=None):
        self.token = str(token or _settings_value("XIAODEFA_TOKEN") or
                         _settings_value("TUSHARE_XIAODEFA_TOKEN")).strip()
        self.url = str(url or _settings_value("XIAODEFA_URL", DEFAULT_XIAODEFA_URL)).strip()
        endpoint = urlsplit(self.url)
        if (endpoint.scheme != "https" or endpoint.hostname != "t.xiaodefa.top"
                or endpoint.port not in (None, 443) or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/")):
            raise XiaodefaError("unapproved xiaodefa endpoint; no credential sent")
        if not self.token:
            raise XiaodefaError("missing token: set XIAODEFA_TOKEN")
        self.min_interval = max(0.0, float(min_interval_seconds))
        self.max_retries = max(1, min(3, int(max_retries)))
        self.timeout = max(0.1, min(60.0, float(timeout)))
        self.max_response_bytes = max(1, min(8_000_000, int(max_response_bytes)))
        self.runner = runner
        self.rate_key = "xiaodefa:" + hashlib.sha256(self.token.encode()).hexdigest()

    def _request(self, body, deadline):
        from trade_system.http_transport import read_verified_once
        from trade_system.host_limiter import shared_host_limiter
        shared_host_limiter.acquire(self.rate_key, self.min_interval, deadline=deadline)
        request = urllib.request.Request(self.url,
            data=json.dumps(body, allow_nan=False, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"})
        try:
            raw = read_verified_once(request, timeout=deadline-time.monotonic(), max_bytes=self.max_response_bytes)
        except ValueError as exc:
            raise XiaodefaError(str(exc)) from None
        self._response_fingerprint = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise XiaodefaError("invalid JSON response") from exc

    def query_data(self, api_name, params=None, fields="", *, _deadline=None):
        """Preserve the native envelope for receipt consumers, without a second HTTP client."""
        body = {"api_name": api_name, "token": self.token,
                "params": dict(params or {}), "fields": fields}
        deadline = time.monotonic() + self.timeout
        if _deadline is not None:
            deadline = min(deadline, _deadline)
        payload = None
        self._response_fingerprint = {}
        for attempt in range(self.max_retries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise XiaodefaError("request deadline exhausted")
            try:
                payload = self.runner(body, remaining) if self.runner else self._request(body, deadline)
                if time.monotonic() >= deadline:
                    raise XiaodefaError("request deadline exhausted")
                break
            except urllib.error.HTTPError as exc:
                from trade_system.http_transport import diagnostic_state
                if diagnostic_state.get() is not None:
                    raise XiaodefaError(f"HTTP {exc.code}; explicit diagnostic comparison required") from None
                if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 == self.max_retries:
                    raise XiaodefaError(f"HTTP {exc.code}; request failed") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                from trade_system.http_transport import diagnostic_state
                if diagnostic_state.get() is not None:
                    raise XiaodefaError('diagnostic transport failed; no automatic retry') from None
                if attempt + 1 == self.max_retries:
                    raise XiaodefaError("transport retry budget exhausted") from None
            delay = min(2 ** attempt, 4)
            if delay >= deadline - time.monotonic():
                raise XiaodefaError("request deadline exhausted")
            time.sleep(delay)
        def fail(reason):
            # Preserve safe structural evidence, never provider text, row values,
            # request bodies or credentials. HTTP fingerprints bind exact bytes.
            data = payload.get('data') if isinstance(payload, dict) else None
            names = data.get('fields') if isinstance(data, dict) else None
            items = data.get('items') if isinstance(data, dict) else None
            diagnostic = dict(self._response_fingerprint, api=api_name,
                envelope_type=type(payload).__name__, data_type=type(data).__name__,
                fields_type=type(names).__name__, items_type=type(items).__name__,
                fields_count=len(names) if isinstance(names, list) else None,
                items_count=len(items) if isinstance(items, list) else None)
            if isinstance(payload, dict) and type(payload.get('code')) is int:
                diagnostic['provider_code'] = payload['code']
            from trade_system.logging_setup import get_logger
            get_logger(__name__).warning('response_contract %s', json.dumps(diagnostic, sort_keys=True))
            error = XiaodefaError(reason)
            error.diagnostic = diagnostic
            raise error
        if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
            from trade_system.http_transport import stop_diagnostic
            stop_diagnostic('business_rejected')
            fail("provider rejected request; no semantic retry")
        data = payload.get("data")
        if not isinstance(data, dict):
            fail("response data missing")
        names, items = data.get("fields"), data.get("items")
        if names == [] and items == []:
            fail('empty response without fields; source data unavailable')
        if (not isinstance(names, list) or not names or any(not isinstance(n,str) or not n for n in names)
                or len(names) != len(set(names)) or not isinstance(items,list)):
            fail("invalid fields/items contract")
        if fields and names != fields.split(","):
            fail("response fields differ from requested projection")
        for row in items:
            if not isinstance(row,list) or len(row) != len(names):
                fail("response row width mismatch")
            if any(isinstance(v,float) and not math.isfinite(v) for v in row):
                fail("non-finite response value")
        return data

    def query_rows(self, api_name, params=None, fields="", *, _deadline=None):
        data = (self.query_data(api_name, params, fields) if _deadline is None
                else self.query_data(api_name, params, fields, _deadline=_deadline))
        return [dict(zip(data["fields"],row)) for row in data["items"]]

    def query(self, api_name, **params):
        fields = params.pop("fields", "")
        deadline = params.pop("_deadline", None)
        return (self.query_rows(api_name, params, fields) if deadline is None
                else self.query_rows(api_name, params, fields, _deadline=deadline))

    def query_all(self, api_name, *, page_size=DEFAULT_PAGE_SIZE,
                  max_rows=MAX_ROWS_SAFETY, on_page=None, total_timeout=None, **params):
        if page_size <= 0 or max_rows <= 0:
            raise XiaodefaError("positive pagination budget required")
        if total_timeout is not None and (not isinstance(total_timeout, (int, float))
                or not 0 < total_timeout <= 60):
            raise XiaodefaError('pagination total timeout must be within 60 seconds')
        deadline = time.monotonic() + (self.timeout if total_timeout is None else total_timeout)
        collected, seen = [], set()
        while len(collected) < max_rows:
            limit = min(page_size, max_rows-len(collected))
            batch = self.query(api_name, limit=limit, offset=len(collected), _deadline=min(deadline, time.monotonic()+self.timeout), **params)
            if on_page is not None:
                on_page(len(collected), batch)
            signature = json.dumps(batch,sort_keys=True,allow_nan=False)
            if batch and signature in seen:
                raise XiaodefaError("repeated page; completeness unknown")
            seen.add(signature)
            if len(batch) > limit:
                raise XiaodefaError("provider exceeded requested page size")
            collected.extend(batch)
            if len(batch) < limit:
                return collected
        raise XiaodefaError("pagination budget exhausted; completeness unknown")
