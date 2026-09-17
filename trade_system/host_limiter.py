"""Cross-process host rate limiting backed by the shared SQLite cache DB."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager


DEFAULT_DB = Path(__file__).resolve().parent / ".stock_cache" / "resilient.db"


class SharedHostLimiter:
    """Coordinate request starts across independent collector processes.

    Threads and independent collectors share one timestamp ledger. SQLite's
    ``BEGIN IMMEDIATE`` gives us a small cross-process lease without adding a
    service dependency.  Set ``KPL_SHARED_RATE_LIMIT=0`` only for isolated
    unit tests.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or os.getenv("KPL_SHARED_RATE_DB", str(DEFAULT_DB)))

    @staticmethod
    def _remaining(deadline):
        from trade_system.http_transport import request_deadline
        if request_deadline.get() is not None:
            deadline = min(deadline, request_deadline.get())
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('shared rate limit deadline exhausted')
        return remaining

    @contextmanager
    def _transaction(self, deadline):
        self._remaining(deadline)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=self._remaining(deadline))
        try:
            con.execute('BEGIN IMMEDIATE')
            self._remaining(deadline)
            con.execute(
                """CREATE TABLE IF NOT EXISTS host_rate_limit (
                       host TEXT PRIMARY KEY,
                       last_started REAL NOT NULL DEFAULT 0,
                       cooldown_until REAL NOT NULL DEFAULT 0,
                       updated_at REAL NOT NULL DEFAULT 0
                   )"""
            )
            yield con
            con.execute(f'PRAGMA busy_timeout={int(self._remaining(deadline)*1000)}')
            con.commit()
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                raise TimeoutError('shared rate limit database wait exhausted') from None
            raise
        finally:
            con.close()

    @property
    def enabled(self) -> bool:
        return os.getenv("KPL_SHARED_RATE_LIMIT", "1").strip().lower() not in {"0", "false", "off"}

    def acquire(self, host: str, min_interval: float, *, deadline: float | None = None) -> None:
        if not self.enabled or min_interval <= 0:
            return
        deadline = deadline if deadline is not None else time.monotonic()+30
        host = str(host or "unknown")
        while True:
            wait = 0.0
            with self._transaction(deadline) as con:
                now = time.time()
                row = con.execute(
                    "SELECT last_started,cooldown_until FROM host_rate_limit WHERE host=?",
                    [host],
                ).fetchone()
                last_started, cooldown_until = row if row else (0.0, 0.0)
                wait = max(float(min_interval) - (now - float(last_started or 0)),
                           float(cooldown_until or 0) - now, 0.0)
                if wait <= 0:
                    con.execute(
                        "INSERT INTO host_rate_limit(host,last_started,cooldown_until,updated_at) VALUES(?,?,0,?) "
                        "ON CONFLICT(host) DO UPDATE SET last_started=excluded.last_started,updated_at=excluded.updated_at",
                        [host, now, now],
                    )
            if wait <= 0:
                return
            if wait >= self._remaining(deadline):
                raise TimeoutError('shared cooldown exceeds request deadline')
            time.sleep(min(wait, 2.0))

    def cooldown(self, host: str, seconds: float, *, deadline: float | None = None) -> None:
        if not self.enabled or seconds <= 0:
            return
        with self._transaction(deadline if deadline is not None else time.monotonic()+30) as con:
            now = time.time()
            until = now + float(seconds)
            con.execute(
                "INSERT INTO host_rate_limit(host,last_started,cooldown_until,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(host) DO UPDATE SET cooldown_until=max(host_rate_limit.cooldown_until,excluded.cooldown_until),updated_at=excluded.updated_at",
                [str(host or "unknown"), 0.0, until, now],
            )


shared_host_limiter = SharedHostLimiter()
