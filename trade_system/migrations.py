"""Incremental schema migration runner for the stock_data project.

``schema.init_schema`` remains the full bootstrap for NEW databases: it is
idempotent and safe to replay on every run.  Any *change* to an existing
production database must instead be expressed as a numbered migration file
under ``migrations/`` so it is applied exactly once per database.

Conventions
-----------
- File name: ``NNNN_short_description.sql`` where ``NNNN`` is a zero-padded,
  monotonically increasing version.  Version ``1`` is reserved for the
  initial P0-P2 contract recorded by ``init_schema`` itself.
- Each file contains plain SQL statements (multiple statements allowed) that
  are executed inside one transaction.  Write them defensively
  (``IF NOT EXISTS`` / ``IF EXISTS``) because they run against databases in
  several historical shapes.
- The applied version is recorded in the existing ``schema_migration`` table;
  re-running the runner skips already-applied versions.
- Non-numbered files (e.g. this README) are ignored.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import duckdb

from trade_system.logging_setup import get_logger

logger = get_logger(__name__)

_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

_FILENAME_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")


def discover_migrations(directory: str | Path = _DEFAULT_MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs sorted by version."""
    root = Path(directory)
    if not root.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in root.iterdir():
        match = _FILENAME_RE.match(path.name)
        if match and path.is_file():
            found.append((int(match.group(1)), path))
    found.sort()
    versions = [version for version, _ in found]
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration versions in {root}: {versions}")
    return found


def ensure_migration_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT current_timestamp,
            description VARCHAR
        )
        """
    )


def applied_versions(con: duckdb.DuckDBPyConnection) -> set[int]:
    ensure_migration_table(con)
    rows = con.execute("SELECT version FROM schema_migration").fetchall()
    return {int(row[0]) for row in rows}


def apply_pending(
    con: duckdb.DuckDBPyConnection,
    directory: str | Path = _DEFAULT_MIGRATIONS_DIR,
    *,
    dry_run: bool = False,
) -> list[int]:
    """Apply every not-yet-applied migration in order; return versions applied.

    Each migration runs inside its own transaction together with the
    ``schema_migration`` bookkeeping row, so a failed migration leaves no
    partial state behind.
    """
    pending = [
        (version, path)
        for version, path in discover_migrations(directory)
        if version not in applied_versions(con)
    ]
    applied: list[int] = []
    for version, path in pending:
        script = path.read_text(encoding="utf-8")
        description = path.stem
        if dry_run:
            logger.info("dry-run migration %s (%s)", version, description)
            applied.append(version)
            continue
        logger.info("applying migration %s (%s)", version, description)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(script)
            con.execute(
                "INSERT INTO schema_migration(version, description) VALUES (?, ?)",
                [version, description],
            )
            con.execute("COMMIT")
        except Exception as exc:
            con.execute("ROLLBACK")
            raise RuntimeError(f"migration {version} ({path.name}) failed: {exc}") from exc
        applied.append(version)
        logger.info("applied migration %s", version)
    return applied


def pending_versions(
    directory: str | Path = _DEFAULT_MIGRATIONS_DIR,
    con: duckdb.DuckDBPyConnection | None = None,
) -> Iterable[int]:
    if con is None:
        return []
    done = applied_versions(con)
    return [v for v, _ in discover_migrations(directory) if v not in done]
