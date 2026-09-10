"""Configuration for KPL data collection system.

Canonical home of project configuration.  The root-level ``config.py`` is a
compatibility shim re-exporting these names.
"""
import os
import warnings
from datetime import datetime
from pathlib import Path


# This file lives in trade_system/, so the project root is one level up.
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _parse_dotenv(path: Path) -> dict:
    values: dict[str, str] = {}
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
    # Keep provider settings in the same precedence chain as KPL settings.  A
    # few older local .env files used ``deepseek-v4-flash`` as the key while
    # storing the token as its value; that spelling is retained as a read-only
    # compatibility alias and is never written back to disk.
    merged.update({
        key: value
        for key, value in os.environ.items()
        if (
            key.startswith("KPL_")
            or key.startswith("TUSHARE_")
            or key.startswith("DEEPSEEK_")
            or key.startswith("XIAODEFA_")
            or key.startswith("HITHINK_")
        )
    })
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


def _get_deepseek(name: str, default: str = "") -> str:
    """Read DeepSeek configuration without exposing or rewriting secrets.

    ``DEEPSEEK_API_KEY`` is the supported name.  The historical project-local
    key ``deepseek-v4-flash`` is accepted only as a deprecated fallback so an
    existing operator .env keeps working until it is migrated.
    """
    value = SETTINGS.get(name, "")
    if not value and name == "DEEPSEEK_API_KEY":
        value = SETTINGS.get("deepseek-v4-flash", "")
        if value:
            warnings.warn(
                ".env uses the deprecated 'deepseek-v4-flash' key name for the "
                "DeepSeek API key; rename it to DEEPSEEK_API_KEY.",
                DeprecationWarning,
                stacklevel=2,
            )
    return str(value or default).strip()


API_BASE = _get("KPL_API_BASE", "https://www.kpl-api.cn/api")
API_KEY = _get("KPL_API_KEY", "")
DB_PATH = _get("KPL_DB_PATH", str(PROJECT_DIR / "kpl_data.duckdb"))
# Operator-facing concept/member queries use 同花顺 first.  KPL remains a
# per-date compatibility fallback in v_default_concept_* views.
DEFAULT_CONCEPT_SOURCE = "ths"

REQUEST_TIMEOUT = int(float(_get("KPL_REQUEST_TIMEOUT", "30")))
REQUEST_DELAY = float(_get("KPL_REQUEST_DELAY", "0.3"))   # seconds between requests
MAX_RETRIES = int(float(_get("KPL_MAX_RETRIES", "5")))
RETRY_DELAY = float(_get("KPL_RETRY_DELAY", "3.0"))     # base retry delay
# Scheduled SYSTEM tasks may use a different certificate store than the
# interactive account. Keep verification on; use an operator-installed CA
# bundle only when the Windows trust store lacks the provider root.
KPL_SSL_CA_BUNDLE = _get("KPL_SSL_CA_BUNDLE", "")
KPL_SSL_USE_SYSTEM_STORE = _get("KPL_SSL_USE_SYSTEM_STORE", "1")
# The local interactive proxy currently presents an untrusted interception
# root, while the provider is reachable directly with normal verification.
# Prefer verified direct transport and fall back to the configured proxy only
# when direct networking is unavailable.
KPL_DIRECT_FIRST = _get("KPL_DIRECT_FIRST", "1")

# Optional AI review provider.  The daily review remains deterministic when
# the key is absent or the provider is unavailable.
DEEPSEEK_API_KEY = _get_deepseek("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _get_deepseek("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = _get_deepseek("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT = float(_get_deepseek("DEEPSEEK_TIMEOUT", "45"))
DEEPSEEK_MAX_RETRIES = int(float(_get_deepseek("DEEPSEEK_MAX_RETRIES", "2")))

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


def default_trade_date(db_path: str | Path | None = None) -> str:
    """Resolve a default trading date without treating a weekend as a session.

    Explicit CLI dates remain authoritative.  This helper is for unattended
    defaults only; if a local calendar is unavailable it returns the natural
    date so the caller can surface the calendar gate rather than silently
    relabelling old market data.
    """
    if db_path:
        try:
            from trade_system.trading_calendar import latest_open_session

            resolved = latest_open_session(db_path)
            if resolved:
                return resolved
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d")

# Logging
LOG_DIR = str(PROJECT_DIR / "logs")
