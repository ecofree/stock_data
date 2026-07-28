"""Cross-process host rate limiting backed by the shared SQLite cache DB."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time


DEFAULT_DB = Path(__file__).resolve().parent / ".stock_cache" / "resilient.db"


class SharedHostLimiter:
    """Coordinate request starts across independent collector processes.

    The old in-memory limiters remain useful inside a process, but they cannot
    see a manually started collector or a scheduled task.  SQLite's
    ``BEGIN IMMEDIATE`` gives us a small cross-process lease without adding a
    service dependency.  Set ``KPL_SHARED_RATE_LIMIT=0`` only for isolated
    unit tests.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or os.getenv("KPL_SHARED_RATE_DB", str(DEFAULT_DB)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=30) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS host_rate_limit (
                       host TEXT PRIMARY KEY,
                       last_started REAL NOT NULL DEFAULT 0,
                       cooldown_until REAL NOT NULL DEFAULT 0,
                       updated_at REAL NOT NULL DEFAULT 0
                   )"""
            )

    @property
    def enabled(self) -> bool:
        return os.getenv("KPL_SHARED_RATE_LIMIT", "1").strip().lower() not in {"0", "false", "off"}

    def acquire(self, host: str, min_interval: float) -> None:
        if not self.enabled or min_interval <= 0:
            return
        host = str(host or "unknown")
        while True:
            now = time.time()
            wait = 0.0
            con = sqlite3.connect(self.db_path, timeout=30)
            try:
                con.execute("BEGIN IMMEDIATE")
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
                con.commit()
            finally:
                con.close()
            if wait <= 0:
                return
            time.sleep(min(wait, 2.0))

    def cooldown(self, host: str, seconds: float) -> None:
        if not self.enabled or seconds <= 0:
            return
        now = time.time()
        until = now + float(seconds)
        with sqlite3.connect(self.db_path, timeout=30) as con:
            con.execute(
                "INSERT INTO host_rate_limit(host,last_started,cooldown_until,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(host) DO UPDATE SET cooldown_until=max(host_rate_limit.cooldown_until,excluded.cooldown_until),updated_at=excluded.updated_at",
                [str(host or "unknown"), 0.0, until, now],
            )


shared_host_limiter = SharedHostLimiter()
