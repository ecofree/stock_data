"""Safe DeepSeek V4 Flash adapter for evidence-based daily review.

The adapter is deliberately outside the trading signal path.  It accepts the
deterministic facts snapshot, asks for JSON, validates evidence references and
returns a structured result.  Provider failures never change readiness or
create an order.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from trade_system.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MAX_RETRIES,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT,
)


class DeepSeekReviewError(RuntimeError):
    """Raised when the provider or its structured response is unusable."""


@dataclass(frozen=True)
class DeepSeekSettings:
    api_key: str = DEEPSEEK_API_KEY
    base_url: str = DEEPSEEK_BASE_URL
    model: str = DEEPSEEK_MODEL
    timeout: float = DEEPSEEK_TIMEOUT
    max_retries: int = DEEPSEEK_MAX_RETRIES


def _json_content(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        raise DeepSeekReviewError("provider returned empty JSON content")
    # Be tolerant of markdown fences while keeping the parsed object strict.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeepSeekReviewError(f"provider returned invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise DeepSeekReviewError("provider JSON must be an object")
    return parsed


def _evidence_ids(snapshot: dict[str, Any]) -> set[str]:
    catalog = snapshot.get("evidence_catalog") or []
    return {
        str(item.get("evidence_id"))
        for item in catalog
        if isinstance(item, dict) and item.get("evidence_id")
    }


def validate_review_output(output: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate the minimal review contract and evidence references."""
    required = ("summary", "risk_flags", "watchlist", "evidence_ids")
    missing = [key for key in required if key not in output]
    if missing:
        raise DeepSeekReviewError("missing review fields: " + ", ".join(missing))
    if not isinstance(output.get("summary"), str) or not output["summary"].strip():
        raise DeepSeekReviewError("summary must be a non-empty string")
    for key in ("risk_flags", "watchlist", "evidence_ids"):
        if not isinstance(output.get(key), list):
            raise DeepSeekReviewError(f"{key} must be a list")
    allowed = _evidence_ids(snapshot)
    referenced = {str(value) for value in output["evidence_ids"]}
    unknown = sorted(referenced - allowed)
    if unknown:
        raise DeepSeekReviewError("unknown evidence_ids: " + ", ".join(unknown[:5]))
    for item in output["watchlist"]:
        if not isinstance(item, dict):
            raise DeepSeekReviewError("watchlist items must be objects")
        item_refs = item.get("evidence_ids", [])
        if not isinstance(item_refs, list) or any(str(value) not in allowed for value in item_refs):
            raise DeepSeekReviewError("watchlist contains unknown evidence_ids")
    return output


class DeepSeekReviewClient:
    def __init__(
        self,
        settings: DeepSeekSettings | None = None,
        *,
        post: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or DeepSeekSettings()
        self._post = post or requests.post
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.settings.api_key and self.settings.base_url and self.settings.model)

    def review(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise DeepSeekReviewError("DeepSeek API key or endpoint is not configured")
        payload = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是A股盘后复盘助手。只根据用户提供的事实快照输出JSON。"
                        "不得补造缺失数据、不得改变任何数据门禁、不得把影子分数变成订单。"
                        "所有数值结论必须用evidence_ids引用事实。输出必须是JSON对象。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "请按以下结构输出JSON：{\"summary\":\"...\","
                        "\"market_view\":\"...\",\"sector_rotation\":[],"
                        "\"stock_observations\":[],\"risk_flags\":[],"
                        "\"watchlist\":[{\"stock_code\":\"\",\"reason\":\"\","
                        "\"evidence_ids\":[]}],\"data_limitations\":[],"
                        "\"evidence_ids\":[]}.\n事实快照："
                        + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), default=str)
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            # V4 defaults to thinking mode.  Daily review uses the fast,
            # deterministic non-thinking path; weekly research can opt in later.
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": 4096,
        }
        last_error: Exception | None = None
        attempts = max(0, int(self.settings.max_retries)) + 1
        for attempt in range(attempts):
            try:
                response = self._post(
                    self.settings.base_url.rstrip("/") + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + self.settings.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.settings.timeout,
                )
                if not response.ok:
                    raise DeepSeekReviewError(f"provider HTTP {response.status_code}")
                body = response.json()
                choices = body.get("choices") or []
                content = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
                parsed = _json_content(content)
                validated = validate_review_output(parsed, snapshot)
                return {
                    "status": "success",
                    "model": str(body.get("model") or self.settings.model),
                    "attempts": attempt + 1,
                    "usage": body.get("usage") or {},
                    "review": validated,
                }
            except (requests.RequestException, DeepSeekReviewError, ValueError, TypeError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._sleep(min(2.0 * (attempt + 1), 5.0))
        raise DeepSeekReviewError(str(last_error or "provider request failed"))
