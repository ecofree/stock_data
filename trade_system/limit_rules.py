"""Single source of truth for A-share price-limit thresholds."""

from __future__ import annotations

from typing import Any


def limit_threshold(code: Any = "", name: Any = "", market: Any = "") -> float:
    """Return the conservative percentage used for limit-up/board checks.

    The values intentionally match the precision-safe project convention:
    4.8/9.8/19.8/29.8.  ST is checked first because a name can be listed on a
    board whose normal limit is higher.
    """
    code_text = "".join(ch for ch in str(code or "") if ch.isdigit())
    name_text = str(name or "").upper()
    market_text = str(market or "")
    if "ST" in name_text:
        return 4.8
    if code_text.startswith(("30", "68")) or market_text in {"创业板", "科创板"}:
        return 19.8
    if code_text.startswith(("8", "4", "92")) or market_text in {"北交所", "北交所A股"}:
        return 29.8
    return 9.8


def limit_threshold_sql(code_expr: str, name_expr: str, market_expr: str) -> str:
    """Build the SQL equivalent used by normalized market views."""
    return (
        f"CASE WHEN upper(coalesce({name_expr},'')) LIKE '%ST%' THEN 4.8 "
        f"WHEN {market_expr} IN ('创业板','科创板') "
        f"OR regexp_matches(CAST({code_expr} AS VARCHAR), '^(30|68)') THEN 19.8 "
        f"WHEN {market_expr} IN ('北交所','北交所A股') "
        f"OR regexp_matches(CAST({code_expr} AS VARCHAR), '^(8|4|92)') THEN 29.8 "
        "ELSE 9.8 END"
    )

