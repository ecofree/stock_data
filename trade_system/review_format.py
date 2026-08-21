"""Pure formatting helpers shared by review queries and renderers."""

from __future__ import annotations

from typing import Any


def _fmt_money(value: Any) -> str:
    """Render canonical yuan amounts compactly without losing sign."""
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 100_000_000:
        return f"{sign}{number / 100_000_000:.2f}亿"
    if number >= 10_000:
        return f"{sign}{number / 10_000:.2f}万"
    return f"{sign}{number:.0f}"


def _fmt_pct(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _format_flow_rows(rows: list[dict], money_fields: tuple[str, ...], pct_fields: tuple[str, ...] = ("change_pct",)) -> list[dict]:
    formatted = []
    for row in rows:
        item = dict(row)
        for field in money_fields:
            if field in item:
                item[field] = _fmt_money(item[field])
        for field in pct_fields:
            if field in item:
                item[field] = _fmt_pct(item[field])
        formatted.append(item)
    return formatted

