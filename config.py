"""Compatibility shim.

The canonical implementation lives in ``trade_system/config.py``.  This
module keeps every historical ``from config import ...`` working while the
package becomes self-contained (no root↔package dependency cycle).
"""
from trade_system.config import *  # noqa: F401,F403
from trade_system.config import (  # noqa: F401  explicit for IDEs/grep
    API_BASE,
    API_KEY,
    DB_PATH,
    DEFAULT_CONCEPT_SOURCE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MAX_RETRIES,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT,
    DUCKDB_MEMORY_LIMIT,
    DUCKDB_TEMP_DIR,
    DUCKDB_THREADS,
    LOG_DIR,
    MAX_EMPTY_RETRIES,
    MAX_RETRIES,
    PROJECT_DIR,
    RATE_LIMIT_DELAY,
    RATE_LIMIT_ENDPOINTS,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    RETRY_DELAY,
    SETTINGS,
    TODAY,
)
