"""Pipeline event helper used by the scheduled runner.

Events:
  start    - heartbeat touch only
  success  - heartbeat touch + optional healthcheck ping + summary notify
  failure  - heartbeat touch (with failure flag) + alert notify

Notification channels and the optional healthcheck URL come from env:
  KPL_NOTIFY_WECHAT_WEBHOOK / KPL_NOTIFY_DINGTALK_WEBHOOK(_SECRET) /
  KPL_NOTIFY_TELEGRAM_TOKEN + KPL_NOTIFY_TELEGRAM_CHAT_ID /
  KPL_NOTIFY_GENERIC_WEBHOOK / KPL_NOTIFY_HEALTHCHECK_URL
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from trade_system import heartbeat, notify  # noqa: E402


def ping_healthcheck(url: str, ok: bool) -> str:
    """Ping a healthchecks.io-style URL; append /fail for failures."""
    try:
        target = url.rstrip("/") + ("" if ok else "/fail")
        urllib.request.urlopen(target, timeout=10)
        return "ok"
    except Exception as exc:
        return f"error: {exc!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, choices=["start", "success", "failure"])
    parser.add_argument("--message", default="")
    args = parser.parse_args()

    heartbeat.write("daily_close", {"event": args.event})
    statuses: dict[str, str] = {}

    # Healthcheck semantics: only completion is pinged.  A start ping would
    # register success before the run finished, defeating the liveness check.
    healthcheck_url = __import__("os").environ.get("KPL_NOTIFY_HEALTHCHECK_URL", "").strip()
    if healthcheck_url and args.event != "start":
        statuses["healthcheck"] = ping_healthcheck(
            healthcheck_url, ok=args.event == "success"
        )

    if args.event == "failure":
        statuses.update(notify.send_text(
            "[stock_data] 每日管道失败",
            args.message or "close run failed; check logs/scheduled_close_*.log",
        ))
    elif args.message:
        statuses.update(notify.send_text("[stock_data]", args.message))

    for key, value in statuses.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
