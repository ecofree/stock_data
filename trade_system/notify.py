"""Outbound notifications for pipeline events (WeChat Work/DingTalk/Telegram/generic).

Design rules:
- Configuration comes purely from environment variables, so nothing sensitive
  lands in the repo.
- ``send_text`` NEVER raises: notification failures are logged, never allowed
  to kill a trading-data run.
- Every dispatch is appended to ``logs/notifications_<date>.log`` so pushes
  have an auditable trail even when no channel is configured.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trade_system.logging_setup import get_logger

logger = get_logger(__name__)

_WECHAT_WEBHOOK_ENV = "KPL_NOTIFY_WECHAT_WEBHOOK"
_DINGTALK_WEBHOOK_ENV = "KPL_NOTIFY_DINGTALK_WEBHOOK"
_DINGTALK_SECRET_ENV = "KPL_NOTIFY_DINGTALK_SECRET"
_TELEGRAM_TOKEN_ENV = "KPL_NOTIFY_TELEGRAM_TOKEN"
_TELEGRAM_CHAT_ENV = "KPL_NOTIFY_TELEGRAM_CHAT_ID"
_GENERIC_WEBHOOK_ENV = "KPL_NOTIFY_GENERIC_WEBHOOK"

_TIMEOUT_SECONDS = 10


def _log_dir() -> Path | None:
    try:
        from trade_system.config import LOG_DIR
        return Path(LOG_DIR)
    except Exception:
        return None


def _post_json(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:200]
        return True, body
    except Exception as exc:
        return False, repr(exc)


def _dingtalk_signed_url(webhook: str, secret: str) -> str:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = quote(base64.b64encode(digest).decode("utf-8"), safe="")
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={timestamp}&sign={sign}"


def channel_payloads(title: str, body: str) -> dict[str, tuple[str, dict[str, Any]]]:
    """Build (url, payload) per configured channel. Exposed for unit tests."""
    text = f"{title}\n{body}".strip()
    out: dict[str, tuple[str, dict[str, Any]]] = {}

    wechat = os.environ.get(_WECHAT_WEBHOOK_ENV, "").strip()
    if wechat:
        out["wechat_work"] = (wechat, {"msgtype": "text", "text": {"content": text[:2000]}})

    dingtalk = os.environ.get(_DINGTALK_WEBHOOK_ENV, "").strip()
    if dingtalk:
        secret = os.environ.get(_DINGTALK_SECRET_ENV, "").strip()
        url = _dingtalk_signed_url(dingtalk, secret) if secret else dingtalk
        out["dingtalk"] = (url, {"msgtype": "text", "text": {"content": text[:2000]}})

    tg_token = os.environ.get(_TELEGRAM_TOKEN_ENV, "").strip()
    tg_chat = os.environ.get(_TELEGRAM_CHAT_ENV, "").strip()
    if tg_token and tg_chat:
        out["telegram"] = (
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            {"chat_id": tg_chat, "text": text[:4000]},
        )

    generic = os.environ.get(_GENERIC_WEBHOOK_ENV, "").strip()
    if generic:
        out["generic"] = (generic, {"title": title, "body": body})

    return out


def send_text(title: str, body: str) -> dict[str, str]:
    """Fan the message out to every configured channel; returns per-channel status."""
    results: dict[str, str] = {}
    payloads = channel_payloads(title, body)
    if not payloads:
        results["none_configured"] = "ok"
    for channel, (url, payload) in payloads.items():
        ok, detail = _post_json(url, payload)
        results[channel] = "ok" if ok else f"error: {detail}"
        if not ok:
            logger.warning("notification channel %s failed: %s", channel, detail)

    log_dir = _log_dir()
    if log_dir:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            entry = (
                f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{title}\t{body!r}\t{results}\n"
            )
            with open(log_dir / f"notifications_{datetime.now():%Y-%m-%d}.log",
                      "a", encoding="utf-8") as fh:
                fh.write(entry)
        except OSError:
            logger.warning("could not write notification audit log", exc_info=True)
    return results
