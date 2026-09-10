"""Local bounded JSONL command interface; no HTTP listener or broker route."""
import argparse
from datetime import datetime
import json
import sys

from .service import Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='Independent V2 database; legacy files are refused')
    args = parser.parse_args()
    with Service(args.db) as service:
        while True:
            line = sys.stdin.buffer.readline(1024*1024 + 1)
            if not line:
                break
            if len(line) > 1024*1024:
                raise ValueError('command exceeds 1MB; input was not submitted')
            try:
                request = json.loads(line)
                payload = request.get('payload', {})
                if 'raw' in payload and isinstance(payload['raw'], str):
                    payload['raw'] = payload['raw'].encode('utf-8')
                result = service.submit(request['command'], **payload).result(timeout=35)
                response = {'ok': True, 'result': result}
            except Exception as exc:
                response = {'ok': False, 'error_type': type(exc).__name__,
                            'reason': str(exc), 'execution_ready': False}
            print(json.dumps(response, ensure_ascii=False, allow_nan=False,
                             default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
