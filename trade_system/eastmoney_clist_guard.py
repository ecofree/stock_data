"""Low-frequency circuit breaker for Eastmoney's live ``clist`` route.

Eastmoney sometimes resets the TCP connection for ``/api/qt/clist/get`` while
other Eastmoney routes remain healthy.  Retrying every numeric front door in a
tight loop makes the block worse.  This small process-safe (best effort)
breaker records a failed full-page probe and asks callers to wait before
trying the route again.  The state lives in the ignored runtime cache, not in
the DuckDB business tables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any


class EastmoneyClistUnavailable(RuntimeError):
    """Raised when the clist circuit is cooling down after upstream resets."""


class EastmoneyClistGuard:
    _lock = Lock()
    _cooldowns = (30, 60, 120, 300, 900)

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get(
            "EASTMONEY_CLIST_GUARD_PATH",
            Path(__file__).resolve().parent / ".stock_cache" / "eastmoney_clist_guard.json",
        ))

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {"failures": 0}

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="eastmoney-clist-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def assert_available(self) -> None:
        if os.environ.get("EASTMONEY_CLIST_BYPASS_GUARD") == "1":
            return
        with self._lock:
            state = self._read()
            blocked_until = state.get("blocked_until")
            if not blocked_until:
                return
            try:
                until = datetime.fromisoformat(str(blocked_until))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
            except ValueError:
                return
            remaining = (until - self._now()).total_seconds()
            if remaining > 0:
                raise EastmoneyClistUnavailable(
                    f"Eastmoney clist circuit cooling down for {int(remaining) + 1}s; "
                    f"last_error={state.get('last_error') or 'transport reset'}"
                )

    def record_failure(self, error: object, endpoint: str | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            failures = int(state.get("failures") or 0) + 1
            seconds = self._cooldowns[min(failures - 1, len(self._cooldowns) - 1)]
            now = self._now()
            state.update({
                "failures": failures,
                "blocked_until": (now + timedelta(seconds=seconds)).isoformat(),
                "last_failed_at": now.isoformat(),
                "last_error": str(error)[:500],
                "last_endpoint": endpoint,
            })
            self._write(state)
            return state

    def record_success(self, endpoint: str | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            now = self._now()
            state = {
                "failures": 0,
                "blocked_until": None,
                "last_success_at": now.isoformat(),
                "last_endpoint": endpoint,
            }
            self._write(state)
            return state


_CACHE_DIR = Path(__file__).resolve().parent / ".stock_cache"

# Keep the primary and delayed front doors isolated.  A provider-side reset on
# push2 must not suppress a healthy push2delay probe, and a delayed-route
# failure must not extend the primary route's cooldown.
PRIMARY_CLIST_GUARD = EastmoneyClistGuard()
DELAY_CLIST_GUARD = EastmoneyClistGuard(
    os.environ.get(
        "EASTMONEY_DELAY_CLIST_GUARD_PATH",
        _CACHE_DIR / "eastmoney_clist_delay_guard.json",
    )
)

# Backwards-compatible name for callers that explicitly mean the primary
# route.  New fallback-aware code should import both named guards above.
DEFAULT_CLIST_GUARD = PRIMARY_CLIST_GUARD
