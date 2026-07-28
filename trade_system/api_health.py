"""API access preflight helpers."""

from __future__ import annotations


def require_api_key(api_key: str | None) -> str:
    key = (api_key or "").strip()
    if not key or key.lower() in {
        "replace-me",
        "replace-with-local-key",
        "your-api-key",
        "your_api_key",
    }:
        raise RuntimeError(
            "KPL_API_KEY is required for live API collection. "
            "Create D:/accio/stock_data/.env from .env.example and set KPL_API_KEY."
        )
    return key
