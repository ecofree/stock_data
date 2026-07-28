from __future__ import annotations

import argparse
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import API_BASE, API_KEY
from trade_system.api_health import require_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Check KPL API access without printing secrets.")
    parser.add_argument("--endpoint", default="/daily")
    parser.add_argument("--date", default="2026-07-06")
    args = parser.parse_args()

    try:
        key = require_api_key(API_KEY)
    except RuntimeError as exc:
        print(f"API access: missing_key message={exc}")
        return 2

    url = f"{API_BASE}{args.endpoint}?{urllib.parse.urlencode({'date': args.date})}"
    req = urllib.request.Request(url, headers={"accept": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(300).decode("utf-8", "replace").replace("\n", " ")
            print(f"API access: status={resp.status} endpoint={args.endpoint} body_sample={body[:220]}")
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace").replace("\n", " ")
        print(f"API access: http={exc.code} endpoint={args.endpoint} body_sample={body[:220]}")
        return 1
    except Exception as exc:
        print(f"API access: error={type(exc).__name__} message={str(exc)[:220]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
