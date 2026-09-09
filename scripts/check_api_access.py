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
from trade_system.http_transport import classify_transport_error, open_verified, ssl_context_note


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
        with open_verified(req, timeout=20) as resp:
            # Do not write provider text to a GBK console.  The old body
            # sample could turn a valid response into a UnicodeEncodeError;
            # status/content-type/byte count are sufficient for this canary.
            body = resp.read()
            print(
                f"API access: status={resp.status} endpoint={args.endpoint} "
                f"content_type={resp.headers.get('content-type', '')} bytes={len(body)}"
            )
            return 0
    except urllib.error.HTTPError as exc:
        exc.read(300)
        print(f"API access: http={exc.code} endpoint={args.endpoint}")
        return 1
    except urllib.error.URLError as exc:
        print(
            f"API access: error={classify_transport_error(exc)} "
            f"transport={ssl_context_note()}"
        )
        return 1
    except Exception as exc:
        print(f"API access: error={type(exc).__name__} message={str(exc)[:220]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
