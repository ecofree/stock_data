"""Central logging helpers for trade_system.

The package previously had zero logging: degraded paths returned empty
results silently, which made data outages indistinguishable from quiet
markets.  This module gives every module a namespaced logger while keeping
library import side-effect free:

- ``get_logger(name)`` returns a child of the ``trade_system`` logger.
- Without any configuration, WARNING+ records still surface on stderr via
  the stdlib ``lastResort`` handler, and they flow into the collector log
  when ``base`` has already configured the root logger.
- Entry-point scripts may call :func:`configure` to attach an explicit
  console/file handler pair (idempotent, env-tunable via KPL_LOG_LEVEL).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

_PACKAGE = "trade_system"

logging.getLogger(_PACKAGE).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``trade_system`` namespace."""
    suffix = name[len(_PACKAGE) + 1:] if name.startswith(f"{_PACKAGE}.") else name
    return logging.getLogger(f"{_PACKAGE}.{suffix}")


def configure(
    *,
    level: int | str | None = None,
    log_file: str | os.PathLike | None = None,
    console: bool | None = None,
) -> logging.Logger:
    """Idempotently attach handlers to the package logger.

    ``level`` defaults to the ``KPL_LOG_LEVEL`` environment variable or INFO.
    ``log_file`` defaults to ``logs/trade_system_<date>.log`` under the
    project log directory.  ``console`` defaults to False when the root
    logger already has handlers (avoids double printing next to base.py's
    basicConfig), otherwise True so standalone report scripts stay visible.
    """
    pkg = logging.getLogger(_PACKAGE)
    if getattr(pkg, "_stock_data_configured", False):
        return pkg

    if level is None:
        env = os.environ.get("KPL_LOG_LEVEL", "").strip().upper()
        level = getattr(logging, env, logging.INFO) if env else logging.INFO

    if console is None:
        console = not logging.getLogger().handlers

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    if console:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        pkg.addHandler(handler)
    if log_file is None:
        try:
            from trade_system.config import LOG_DIR  # lazy import: avoid cycles at module load
            log_file = Path(LOG_DIR) / f"trade_system_{datetime.now():%Y-%m-%d}.log"
        except Exception:
            log_file = None
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            pkg.addHandler(file_handler)
        except OSError:
            pass

    pkg.setLevel(level)
    pkg._stock_data_configured = True  # type: ignore[attr-defined]
    return pkg
