"""Configuration for KPL data collection system."""
import os
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent


def _parse_dotenv(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _settings() -> dict:
    merged = {}
    configured_env = os.environ.get("KPL_ENV_FILE", "").strip()
    paths = (
        [Path(configured_env)]
        if configured_env
        else [PROJECT_DIR / ".env", Path.cwd() / ".env"]
    )
    for path in paths:
        merged.update(_parse_dotenv(path))
    merged.update({key: value for key, value in os.environ.items() if key.startswith("KPL_")})
    return merged


SETTINGS = _settings()


_PLACEHOLDER_API_KEYS = {
    "replace-me",
    "replace-with-local-key",
    "your-api-key",
    "your_api_key",
}


def _get(name: str, default: str) -> str:
    value = SETTINGS.get(name, default)
    if name == "KPL_API_KEY" and value.strip().lower() in _PLACEHOLDER_API_KEYS:
        return ""
    return value


API_BASE = _get("KPL_API_BASE", "https://kpl.liuhepc.cn/api")
API_KEY = _get("KPL_API_KEY", "")
DB_PATH = _get("KPL_DB_PATH", str(PROJECT_DIR / "kpl_data.duckdb"))
# Operator-facing concept/member queries use 同花顺 first.  KPL remains a
# per-date compatibility fallback in v_default_concept_* views.
DEFAULT_CONCEPT_SOURCE = "ths"

REQUEST_TIMEOUT = int(float(_get("KPL_REQUEST_TIMEOUT", "30")))
REQUEST_DELAY = float(_get("KPL_REQUEST_DELAY", "0.3"))   # seconds between requests
MAX_RETRIES = int(float(_get("KPL_MAX_RETRIES", "5")))
RETRY_DELAY = float(_get("KPL_RETRY_DELAY", "3.0"))     # base retry delay

# Rate-limit: extra spacing for heavy endpoints
RATE_LIMIT_ENDPOINTS = ["/sector/ranking", "/sector/stocks", "/sector/strength", "/sector/capital"]
RATE_LIMIT_DELAY = float(_get("KPL_RATE_LIMIT_DELAY", "0.6"))
MAX_EMPTY_RETRIES = int(float(_get("KPL_MAX_EMPTY_RETRIES", "2")))   # fast-fail for empty responses, then use fallback

# DuckDB resource governance (WP2).  DuckDB defaults to ~80% of RAM, every core,
# and no spill directory; with several concurrent collectors sharing the multi-GB
# database that oversubscribes memory and surfaces as "Out of Memory Error:
# Allocation failure".  Bounds are env-tunable.
DUCKDB_MEMORY_LIMIT = _get("KPL_DUCKDB_MEMORY_LIMIT", "6GB")
DUCKDB_THREADS = _get("KPL_DUCKDB_THREADS", "8")
DUCKDB_TEMP_DIR = _get("KPL_DUCKDB_TEMP_DIR", str(PROJECT_DIR / "tmp" / "duckdb_spill"))

TODAY = datetime.now().strftime("%Y-%m-%d")

# Logging
LOG_DIR = str(PROJECT_DIR / "logs")
os.makedirs(LOG_DIR, exist_ok=True)
