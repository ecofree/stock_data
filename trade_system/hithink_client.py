"""Client for the official HiThink (同花顺) A-share financial data API.

Docs: https://fuyao.aicubes.cn/docs/api-reference/
Auth: ``X-api-key`` header, key from ``HITHINK_FINANCE_API_KEY`` (env / .env).
All business errors travel with HTTP 200 and a non-zero ``code`` field, so
every call must check the envelope.

Rate limits are account-dependent; callers should keep a polite interval
(default 0.35s between requests) and expect ``code=5003`` for dates outside
the authorized history window.
"""
from __future__ import annotations

import json
import hashlib
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from trade_system.logging_setup import get_logger
from trade_system.http_transport import read_verified_once
from trade_system.host_limiter import shared_host_limiter

logger = get_logger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"
CST = timezone(timedelta(hours=8))
_DEFAULT_INTERVAL = 0.35


class HiThinkError(RuntimeError):
    pass


class HiThinkClient:
    def __init__(self, api_key: str | None = None,
                 min_interval: float = _DEFAULT_INTERVAL,
                 timeout: float = 30.0, *, max_response_bytes: int = 8_000_000):
        import os

        from trade_system.config import SETTINGS
        self.api_key = (api_key
                        or SETTINGS.get("HITHINK_FINANCE_API_KEY")
                        or os.environ.get("HITHINK_FINANCE_API_KEY", "")
                        ).strip()
        if not self.api_key:
            raise HiThinkError(
                "HITHINK_FINANCE_API_KEY is not configured (.env or env)")
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('timeout must be finite and within 60 seconds')
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError('interval must be finite and nonnegative')
        self.min_interval = min_interval
        self.timeout = timeout
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 8_000_000:
            raise ValueError('response budget must be 1..8000000 bytes')
        self.max_response_bytes = max_response_bytes
        self.rate_key = 'hithink:' + hashlib.sha256(self.api_key.encode()).hexdigest()
        self.call_count = 0

    @property
    def quota_note(self) -> str:
        """Per-instance call counter for quota observation."""
        return f"hithink calls this session: {self.call_count}"

    # ------------------------------------------------------------ transport
    def _get(self, path: str, params: dict[str, Any] | None = None, *, _deadline=None) -> dict:
        if not path.startswith('/api/') or '?' in path or '#' in path or '..' in path:
            raise HiThinkError('unapproved API path')
        deadline = min(time.monotonic() + self.timeout, _deadline) if _deadline is not None else time.monotonic() + self.timeout
        shared_host_limiter.acquire(self.rate_key, self.min_interval, deadline=deadline)
        url = BASE_URL + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'X-api-key': self.api_key})
        try:
            raw = read_verified_once(req, timeout=deadline-time.monotonic(), max_bytes=self.max_response_bytes)
            payload = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise HiThinkError('invalid or over-budget native response') from exc
        finally:
            self.call_count += 1
        if time.monotonic() >= deadline:
            raise HiThinkError('native request deadline exhausted')
        if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
            raise HiThinkError('provider rejected request; no semantic retry')
        data = payload.get('data')
        if not isinstance(data, dict):
            raise HiThinkError('native response data missing')
        return data

    @staticmethod
    def _date_ms(date_str: str) -> int:
        dt = datetime.strptime(date_str.replace("-", ""), "%Y%m%d").replace(
            hour=0, tzinfo=CST)
        return int(dt.timestamp() * 1000)

    def _paged(self, path: str, params: dict[str, Any],
               item_key: str = "item", max_pages: int = 20) -> list[dict]:
        """Fetch every page of a paginated endpoint; returns combined items."""
        if type(max_pages) is not int or not 1 <= max_pages <= 50:
            raise HiThinkError('page budget must be 1..50')
        deadline = time.monotonic() + self.timeout
        out: list[dict] = []
        seen = set()
        expected_pages = None
        for page in range(1, max_pages + 1):
            data = self._get(path, {**params, 'page': page}, _deadline=deadline)
            pagination = data.get('pagination')
            items = data.get(item_key)
            if not isinstance(pagination, dict) or not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
                raise HiThinkError('invalid pagination contract')
            pages = pagination.get('pages')
            if type(pages) is not int or not 1 <= pages <= max_pages:
                raise HiThinkError('unknown or exhausted page budget')
            if expected_pages is not None and pages != expected_pages:
                raise HiThinkError('pagination changed during collection')
            expected_pages = pages
            fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if fingerprint in seen or (not items and pages > 1):
                raise HiThinkError('repeated or missing page; collection incomplete')
            seen.add(fingerprint)
            out.extend(items)
            if page == pages:
                return out
        raise HiThinkError('page budget exhausted')

    # ------------------------------------------------------- special data
    def limit_up_pool(self, date_str: str, max_pages: int = 20) -> list[dict]:
        """涨停/连板股票池。date_str: YYYY-MM-DD 或 YYYYMMDD."""
        return self._paged(
            "/api/a-share/special-data/limit-up-pool",
            {"date_ms": self._date_ms(date_str), "size": 200,
             "sort_field": "limit_up_time", "sort_dir": "asc"},
            max_pages=max_pages,
        )

    def limit_down_pool(self, date_str: str, max_pages: int = 20) -> list[dict]:
        return self._paged(
            "/api/a-share/special-data/limit-down-pool",
            {"date_ms": self._date_ms(date_str), "size": 200},
            max_pages=max_pages,
        )

    def limit_break_pool(self, date_str: str, max_pages: int = 20) -> list[dict]:
        """炸板池（含 open_times 开板次数）。"""
        return self._paged(
            "/api/a-share/special-data/limit-break-pool",
            {"date_ms": self._date_ms(date_str), "size": 200},
            max_pages=max_pages,
        )

    def limit_up_ladder(self) -> dict:
        """近 30 个交易日连板梯队矩阵（无入参）。"""
        return self._get("/api/a-share/special-data/limit-up-ladder")

    # ------------------------------------------------------------- index
    def ths_concept_catalog(self, tag: str = "cn_concept",
                            max_pages: int = 50) -> list[dict]:
        return self._paged(
            "/api/a-share-index/catalog/ths-index-list",
            {"tag": tag, "size": 200},
            max_pages=max_pages,
        )

    def ths_index_constituents(self, thscode: str, max_pages: int = 50) -> list[dict]:
        return self._paged(
            "/api/a-share-index/constituents/ths-stock-list",
            {"thscode": thscode, "size": 200},
            max_pages=max_pages,
        )

    # -------------------------------------------------------------- misc
    def trading_days(self) -> list[str]:
        data = self._get("/api/a-share/calendar/trading-days")
        return [str(x) for x in (data.get("item") or [])]
