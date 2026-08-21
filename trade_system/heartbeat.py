"""Pipeline heartbeat files for external liveness monitoring.

The scheduled close run writes a heartbeat JSON under ``state/`` after every
attempt.  An external watcher (Task Scheduler on another machine, cron,
healthchecks.io style probe, or simply ``scripts/check_pipeline_heartbeat.py``)
fails loudly when the heartbeat goes stale — because a silently dead scheduler
is the worst failure mode for a daily data pipeline.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from trade_system.logging_setup import get_logger

logger = get_logger(__name__)

STATE_DIR = Path(__file__).resolve().parents[1] / "state"
DEFAULT_MAX_AGE_SECONDS = 26 * 3600  # daily job: allow one missed day


def _path(name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
    return STATE_DIR / f"heartbeat_{safe}.json"


def write(name: str, extra: dict[str, Any] | None = None) -> Path:
    """Record a fresh heartbeat. Returns the file path."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "name": name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "epoch": int(datetime.now().timestamp()),
        "host": socket.gethostname(),
        "pid": os.getpid(),
    }
    if extra:
        payload.update(extra)
    path = _path(name)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def read(name: str) -> dict[str, Any] | None:
    path = _path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("unreadable heartbeat file: %s", path)
        return None


def age_seconds(name: str) -> float | None:
    payload = read(name)
    if not payload or "epoch" not in payload:
        return None
    return max(0.0, datetime.now().timestamp() - float(payload["epoch"]))


def is_stale(name: str, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
    age = age_seconds(name)
    return age is None or age > max_age_seconds
