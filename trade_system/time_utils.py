"""Time normalization for local (Asia/Shanghai) DuckDB timestamps.

Collectors historically write naive timestamps using the machine's local
China time. CLI audit options may carry an explicit offset; normalizing both
sides to the same naive local representation prevents false stale results.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


CHINA_TZ = timezone(timedelta(hours=8))


def as_local_naive(value: datetime | str | None) -> datetime | None:
    """Return a datetime comparable with the project's naive DB timestamps."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError(f"unsupported datetime value: {type(value)!r}")
    if value.tzinfo is not None:
        return value.astimezone(CHINA_TZ).replace(tzinfo=None)
    return value.replace(tzinfo=None)
