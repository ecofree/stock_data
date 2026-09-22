"""Semantic validation shared by provider clients.

Transport success is not data success.  Public market endpoints frequently
return a 200 response containing an empty payload, a previous trading day, or
an all-zero placeholder while the upstream service is rate limiting us.  The
validators in this module deliberately stay dependency-light so they can run
before anything is written to DuckDB.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
import math
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""


_DATE_KEYS = {
    "date", "trade_date", "tradedate", "snapshot_date", "data_date",
    "business_date", "fetched_date", "latest_date", "日期", "交易日期",
}
_DATE_RE = re.compile(r"^\d{4}[-/]?\d{2}[-/]?\d{2}$")


def _normalise_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text[:10].replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text if _DATE_RE.match(text) else ""


def _dates(value: Any, *, depth: int = 0) -> set[str]:
    """Collect explicit date fields without interpreting arbitrary strings."""
    if depth > 4:
        return set()
    out: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key).lower().replace("-", "_")
            if key_name in _DATE_KEYS:
                date_value = _normalise_date(item)
                if date_value:
                    out.add(date_value)
            elif isinstance(item, (Mapping, list, tuple)):
                out.update(_dates(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (Mapping, list, tuple)):
                out.update(_dates(item, depth=depth + 1))
    return out


def _nonempty_sequence(value: Any) -> bool:
    if isinstance(value, (str, bytes)):
        return bool(value)
    return isinstance(value, Iterable) and not isinstance(value, Mapping) and bool(value)


def _all_numeric_zero(value: Any) -> bool:
    numbers: list[float] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key).lower()
            if key_name in {"date", "trade_date", "snapshot_date", "raw_json"}:
                continue
            if isinstance(item, Mapping):
                numbers.extend(_numeric_values(item))
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                numbers.append(float(item))
    return bool(numbers) and all(abs(item) < 1e-12 for item in numbers)


def _realtime_payload_has_rows(endpoint: str, data: Any) -> bool:
    """Accept the concrete shapes used by the live KPL realtime endpoints."""
    if isinstance(data, (list, tuple)):
        return bool(data)
    if not isinstance(data, Mapping):
        return False

    if endpoint == "/l2/realtime/all-boards":
        # The live response is grouped by board level, not wrapped in ``data``.
        keys = (
            "first_board", "second_board", "third_board", "fourth_board",
            "fifth_board", "fifth_board_plus", "gouban", "data", "items", "list",
        )
    elif endpoint == "/l2/realtime/index-list":
        keys = ("indexes", "indices", "data", "items", "list")
    else:
        keys = ("data", "stocks", "items", "list", "ladder")
    return any(_nonempty_sequence(data.get(key)) for key in keys)


def _numeric_values(value: Any) -> list[float]:
    out: list[float] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"date", "trade_date", "snapshot_date", "raw_json"}:
                continue
            if isinstance(item, Mapping):
                out.extend(_numeric_values(item))
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                out.append(float(item))
    return out


def validate_kpl(endpoint: str, params: Mapping[str, Any] | None, data: Any) -> ValidationResult:
    """Validate KPL responses that are used by trading-stage collectors."""
    if data is None:
        return ValidationResult(False, "null response")
    if isinstance(data, Mapping):
        if not data and endpoint not in {"/market/mood"}:
            return ValidationResult(False, "empty object")
    elif isinstance(data, (list, tuple)) and not data:
        return ValidationResult(False, "empty list")

    params = params or {}
    requested = _normalise_date(params.get("date") or params.get("trade_date"))
    returned_dates = _dates(data)
    explicit_dates = {_normalise_date(v) for k, v in data.items()
                      if str(k).lower() in _DATE_KEYS and _normalise_date(v)} if isinstance(data, Mapping) else set()
    if requested and explicit_dates and explicit_dates != {requested}:
        return ValidationResult(False, f"date mismatch requested={requested} returned={sorted(explicit_dates)}")
    if requested and returned_dates and requested not in returned_dates:
        return ValidationResult(False, f"date mismatch requested={requested} returned={sorted(returned_dates)[:3]}")

    if endpoint == "/auction/tick":
        if not isinstance(data, Mapping) or not requested or explicit_dates != {requested}:
            return ValidationResult(False, "auction tick requires explicit matching source date")
        code = str(params.get('code', '')).split('.')[0]
        if not re.fullmatch(r'[0-9]{6}', code) or str(data.get('stock_code', data.get('code', ''))) != code:
            return ValidationResult(False, "auction tick identity mismatch")
        ticks = data.get('auction_ticks')
        if not isinstance(ticks, list) or not 0 < len(ticks) <= 10000:
            return ValidationResult(False, "auction tick rows missing or unbounded")
        previous = ''
        for tick in ticks:
            if not isinstance(tick, Mapping):
                return ValidationResult(False, "auction tick object required")
            stamp = str(tick.get('time', ''))
            if not re.fullmatch(r'09:[0-5][0-9]:[0-5][0-9]', stamp) or not '09:15:00' <= stamp <= '09:25:00' or stamp <= previous:
                return ValidationResult(False, "auction tick time outside window, duplicated or unordered")
            try:
                price, volume = float(tick['price']), float(tick['volume'])
                valid = math.isfinite(price) and price > 0 and math.isfinite(volume) and volume >= 0 and volume.is_integer()
            except (KeyError, TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                return ValidationResult(False, "invalid auction tick price or volume")
            previous = stamp
    elif endpoint == "/daily":
        # The endpoint has returned a 200/all-zero placeholder in production.
        # Reject it only for an explicitly requested date; non-trading-day
        # probes without a date remain valid as an empty market context.
        if requested and _all_numeric_zero(data):
            return ValidationResult(False, "all-zero daily placeholder")
    elif endpoint == "/market/rise-fall":
        raw = data.get("raw_data") if isinstance(data, Mapping) else None
        if requested and isinstance(raw, list):
            if not any(
                isinstance(row, (list, tuple)) and len(row) >= 7
                and _normalise_date(row[6]) == requested
                for row in raw
            ):
                return ValidationResult(False, f"missing requested rise-fall row {requested}")
    elif endpoint == "/sector/ranking":
        sectors = data.get("sectors") if isinstance(data, Mapping) else None
        if not _nonempty_sequence(sectors):
            return ValidationResult(False, "empty sector ranking")
    elif endpoint in {"/sector/capital", "/sector/stocks", "/sector/strength"}:
        if isinstance(data, Mapping) and not data:
            return ValidationResult(False, "empty sector payload")
    elif endpoint in {"/l2/realtime/all-boards", "/ladder/realtime-boards", "/l2/realtime/index-list"}:
        if not _realtime_payload_has_rows(endpoint, data):
            return ValidationResult(False, "empty realtime board payload")
    return ValidationResult(True)
