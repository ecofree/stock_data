"""Shared argparse conventions for scripts/ entry points.

Two spellings of the trade-date flag exist across the codebase
(``--trade-date`` in the runner family, ``--date`` in most focused tools).
New and converted scripts register BOTH via :func:`add_trade_date_argument`
so operators never have to guess; ``dest`` is always ``trade_date``.
"""
from __future__ import annotations

import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def add_db_argument(parser: argparse.ArgumentParser, default: str | Path | None = None) -> None:
    """Register the standard ``--db`` flag (default: project DuckDB path)."""
    if default is None:
        default = PROJECT_ROOT / "kpl_data.duckdb"
    parser.add_argument("--db", default=str(default), help="Path to the DuckDB database.")


def add_trade_date_argument(
    parser: argparse.ArgumentParser,
    *,
    default: str = "",
    help_text: str = "Trade date as YYYY-MM-DD.",
) -> None:
    """Register ``--trade-date`` with ``--date`` accepted as an alias."""
    parser.add_argument(
        "--trade-date",
        "--date",
        dest="trade_date",
        default=default,
        help=help_text,
    )
