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
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from trade_system.logging_setup import get_logger

logger = get_logger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"
CST = timezone(timedelta(hours=8))
_DEFAULT_INTERVAL = 0.35


class HiThinkError(RuntimeError):
    pass


class HiThinkClient:
    def __init__(self, api_key: str | None = None,
                 min_interval: float = _DEFAULT_INTERVAL,
                 timeout: float = 30.0):
        import os

        from trade_system.config import SETTINGS
        self.api_key = (api_key
                        or SETTINGS.get("HITHINK_FINANCE_API_KEY")
                        or os.environ.get("HITHINK_FINANCE_API_KEY", "")
                        ).strip()
        if not self.api_key:
            raise HiThinkError(
                "HITHINK_FINANCE_API_KEY is not configured (.env or env)")
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_call = 0.0
        self.call_count = 0

    @property
    def quota_note(self) -> str:
        """Per-instance call counter for quota observation."""
        return f"hithink calls this session: {self.call_count}"

    # ------------------------------------------------------------ transport
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-api-key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            self._last_call = time.time()
            self.call_count += 1
        code = payload.get("code")
        if code != 0:
            raise HiThinkError(f"{path} -> code={code} message={payload.get('message')}")
        return payload.get("data") or {}

    @staticmethod
    def _date_ms(date_str: str) -> int:
        dt = datetime.strptime(date_str.replace("-", ""), "%Y%m%d").replace(
            hour=0, tzinfo=CST)
        return int(dt.timestamp() * 1000)

    def _paged(self, path: str, params: dict[str, Any],
               item_key: str = "item", max_pages: int = 20) -> list[dict]:
        """Fetch every page of a paginated endpoint; returns combined items."""
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            data = self._get(path, {**params, "page": page})
            pagination = data.get("pagination") or {}
            items = data.get(item_key) or []
            out.extend(items)
            pages = int(pagination.get("pages") or 1)
            if page >= pages:
                break
            page += 1
        return out

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
