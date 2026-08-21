"""Compatibility shim.

The canonical implementation lives in ``trade_system/schema.py``.  This
module keeps every historical ``from schema import ...`` working while the
package becomes self-contained (no root↔package dependency cycle).
"""
from trade_system.schema import *  # noqa: F401,F403
from trade_system.schema import (  # noqa: F401  explicit for IDEs/grep
    _ensure_business_indexes,
    _refresh_default_concept_views,
    init_schema,
)
