"""Vibe-Research adapter primitives used by the stock_data research layer."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def discover_vibe_news_sources(
    explicit_path: str | Path | None = None,
    vibe_root: str | Path | None = None,
) -> Path | None:
    if explicit_path:
        path = Path(explicit_path)
        return path if path.exists() else None

    candidates: list[Path] = []
    for env_name in ("VIBE_RESEARCH_ROOT", "STOCK_DATA_VIBE_ROOT"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value) / "backend" / "news_sources.json")
    if vibe_root:
        candidates.append(Path(vibe_root) / "backend" / "news_sources.json")

    candidates.extend(
        [
            Path(r"D:\accio\Vibe-Research\backend\news_sources.json"),
            Path(r"D:\accio\stock_data\external\Vibe-Research\backend\news_sources.json"),
            Path(tempfile.gettempdir()) / "stock_data_external_research" / "Vibe-Research" / "backend" / "news_sources.json",
        ]
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def load_vibe_news_config(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    config.setdefault("fetch", {})
    config.setdefault("industries", [])
    config.setdefault("sources", [])
    return config


def radar_skeleton_from_config(config: dict[str, Any]) -> dict[str, Any]:
    by_hint: dict[str, int] = {}
    for source in config.get("sources", []):
        hint = source.get("hint", "")
        by_hint[hint] = by_hint.get(hint, 0) + 1
    return {
        "generated_at": None,
        "recent_days": config.get("fetch", {}).get("recent_days", 7),
        "industries": [
            {
                "key": industry.get("key", ""),
                "name": industry.get("name", ""),
                "accent": industry.get("accent", ""),
                "total": by_hint.get(industry.get("key", ""), 0),
                "items": [],
            }
            for industry in config.get("industries", [])
        ],
        "stats": {
            "industries": len(config.get("industries", [])),
            "total_sources": len(config.get("sources", [])),
        },
    }
