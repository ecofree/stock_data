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
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""


_DATE_KEYS = {
    "date", "trade_date", "tradedate", "snapshot_date", "data_date",
    "business_date", "fetched_date", "latest_date",
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
        for item in value[:200]:
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
    if requested and returned_dates and requested not in returned_dates:
        return ValidationResult(False, f"date mismatch requested={requested} returned={sorted(returned_dates)[:3]}")

    if endpoint == "/daily":
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
