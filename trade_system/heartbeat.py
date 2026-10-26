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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from trade_system.logging_setup import get_logger
from trade_system.file_lock import FileLock

logger = get_logger(__name__)

STATE_DIR = Path(__file__).resolve().parents[1] / "state"
DEFAULT_MAX_AGE_SECONDS = 26 * 3600  # daily job: allow one missed day


def _path(name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
    return STATE_DIR / f"heartbeat_{safe}.json"


def write(name: str, extra: dict[str, Any] | None = None) -> Path:
    """Record an attempt; only an explicit success advances last_success."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    attempt: dict[str, Any] = dict(extra or {})
    attempt.update({
        "name": name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "epoch": int(datetime.now().timestamp()),
        "host": socket.gethostname(),
        "pid": os.getpid(),
    })
    attempt.pop('last_success', None)
    attempt.pop('last_attempt', None)
    attempt['event'] = str(attempt.get('event', 'attempt'))
    path = _path(name)
    with FileLock(path.with_suffix('.guard')):
        prior = read(name) or {}
        success = prior.get('last_success')
        if not success and prior.get('event')=='success' and 'epoch' in prior:
            success = {k:v for k,v in prior.items() if k not in ('last_attempt','last_success')}
        if attempt['event']=='success':
            success = attempt
        payload = {**attempt, 'last_attempt':attempt, 'last_success':success}
        temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
        try:
            with temporary.open('x',encoding='utf-8') as stream:
                json.dump(payload,stream,ensure_ascii=False,indent=1)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def read(name: str) -> dict[str, Any] | None:
    path = _path(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else None
    except (OSError, ValueError):
        logger.warning("unreadable heartbeat file: %s", path)
        return None


def age_seconds(name: str) -> float | None:
    """Age of the latest attempt; this alone is not a completion gate."""
    payload = read(name)
    if not payload or "epoch" not in payload:
        return None
    return max(0.0, datetime.now().timestamp() - float(payload["epoch"]))


def completion_status(name: str, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> str:
    payload = read(name)
    if not payload:
        return 'MISSING'
    attempt = payload.get('last_attempt',payload)
    if not isinstance(attempt,dict):
        return 'INVALID'
    event = attempt.get('event')
    if event=='failure':
        return 'FAILED'
    if event=='start':
        return 'IN_PROGRESS'
    if event!='success':
        return 'INCOMPLETE'
    success = payload.get('last_success',attempt)
    try:
        import math
        epoch = float(success['epoch'])
        age = datetime.now().timestamp()-epoch
        if not math.isfinite(age) or age < -60 or success.get('event')!='success':
            return 'INVALID'
        return 'SUCCESS' if age<=max_age_seconds else 'STALE_SUCCESS'
    except (KeyError,TypeError,ValueError):
        return 'INVALID'


def is_stale(name: str, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
    age = age_seconds(name)
    return age is None or age > max_age_seconds
