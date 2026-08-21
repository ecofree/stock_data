"""Exit nonzero when the pipeline heartbeat is stale.

Intended for an external watcher (second Task Scheduler entry, cron, uptime
probe).  A missing or stale heartbeat means the daily close run has not
completed within its allowed window.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from trade_system import heartbeat, notify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="daily_close")
    parser.add_argument("--max-age-seconds", type=int,
                        default=heartbeat.DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument(
        "--alert-on-stale", action="store_true",
        help="Also push a notification (channels via KPL_NOTIFY_* env) when stale.",
    )
    args = parser.parse_args()

    age = heartbeat.age_seconds(args.name)
    if age is None:
        print(f"heartbeat {args.name}: MISSING")
        if args.alert_on_stale:
            notify.send_text("[stock_data] 心跳缺失",
                             f"heartbeat '{args.name}' does not exist")
        return 1
    if age > args.max_age_seconds:
        print(f"heartbeat {args.name}: STALE ({age/3600:.1f}h > "
              f"{args.max_age_seconds/3600:.1f}h)")
        if args.alert_on_stale:
            notify.send_text("[stock_data] 心跳过期",
                             f"heartbeat '{args.name}' is {age/3600:.1f}h old")
        return 1
    print(f"heartbeat {args.name}: OK ({age/60:.0f}m old)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
