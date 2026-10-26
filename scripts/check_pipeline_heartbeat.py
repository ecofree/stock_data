"""Verify successful completion separately from attempt liveness.

Intended for an external watcher (second Task Scheduler entry, cron, uptime
probe).  A missing or stale heartbeat means the daily close run has not
completed within its allowed window.
"""
from __future__ import annotations

import argparse
import json
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
    parser.add_argument('--runtime-state', type=Path, help='Explicit pause-state JSON; status=paused is neutral, never successful completion.')
    parser.add_argument('--calendar-db', type=Path, help='Read-only exchange calendar for an explicit expected trade date.')
    parser.add_argument('--trade-date', help='Expected YYYY-MM-DD session; requires --calendar-db.')
    parser.add_argument('--run-id', help='Expected collection attempt ID; never a backup receipt ID.')
    parser.add_argument(
        "--alert-on-stale", action="store_true",
        help="Also push a notification (channels via KPL_NOTIFY_* env) when stale.",
    )
    args = parser.parse_args()

    if args.max_age_seconds<=0 or bool(args.calendar_db)!=bool(args.trade_date):
        parser.error('positive age and paired --calendar-db/--trade-date required')
    status = heartbeat.completion_status(args.name,args.max_age_seconds)
    if args.runtime_state:
        try:
            state = json.loads(args.runtime_state.read_text(encoding='utf-8'))
            mode = state['status'].lower()
            if mode not in ('paused','active','running'):
                raise ValueError('unknown runtime status')
        except (OSError,ValueError,KeyError,AttributeError,TypeError):
            print('runtime context: UNVERIFIED')
            return 1
        if mode=='paused':
            print(f'heartbeat {args.name}: PAUSED (completion={status}; no completion expected)')
            return 0
    if args.calendar_db:
        from trade_system.trading_calendar import trading_session_status
        session = trading_session_status(args.calendar_db,args.trade_date)
        if session.state!='open':
            print(f'heartbeat {args.name}: SESSION_{session.state.upper()} (completion={status})')
            return 0 if session.state=='closed' else 1
        payload = heartbeat.read(args.name) or {}
        success = payload.get('last_success') or payload
        if status=='SUCCESS' and success.get('trade_date')!=session.trade_date:
            status = 'WRONG_SESSION'
    if args.run_id and status == 'SUCCESS':
        payload = heartbeat.read(args.name) or {}
        success = payload.get('last_success') or payload
        if success.get('run_id') != args.run_id:
            status = 'WRONG_RUN'
    print(f'heartbeat {args.name}: {status}')
    if status!='SUCCESS' and args.alert_on_stale:
        notify.send_text('[stock_data] 管道完成状态异常', f"heartbeat '{args.name}': {status}")
    return 0 if status=='SUCCESS' else 1


if __name__ == "__main__":
    sys.exit(main())
