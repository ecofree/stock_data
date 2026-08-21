"""Tiny shared helpers used by multiple source adapters and the facade."""
from __future__ import annotations


def _f(d, k):
    try:
        v = d.get(k)
        return float(v) if v not in (None, "", "-") else None
    except Exception:
        return None


def _d(date_fmt="%Y-%m-%d"):
    return __import__("datetime").date.today().strftime(date_fmt)
