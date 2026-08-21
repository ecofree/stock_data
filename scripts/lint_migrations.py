"""Lint migrations/*.sql against the table-naming conventions.

Part of the pre-push gate (scripts/pre_push_check.ps1).  See
docs/table_naming.md for the rationale.  Exit nonzero on any violation.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

BANNED_FRAGMENTS = (
    "zhangting", "dieting", "fengban", "gudong", "chouma", "dingpan",
    "fengk", "weipan", "qiangchou", "xianhuo", "zjmm", "fenbi", "duidao",
    "tuoyadan", "gujia", "bkjj", "youzi", "jingjia", "liutong", "huanshou",
)

_FILENAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


def lint_file(path: Path) -> list[str]:
    problems: list[str] = []
    if not _FILENAME_RE.match(path.name):
        problems.append(f"{path.name}: filename must match NNNN_lower_snake_description.sql")
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    for fragment in BANNED_FRAGMENTS:
        if fragment in lowered:
            problems.append(
                f"{path.name}: banned pinyin fragment '{fragment}' "
                "(see docs/table_naming.md)"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint schema migration files.")
    parser.add_argument(
        "--migrations-dir",
        default=str(DEFAULT_MIGRATIONS_DIR),
        help="Directory containing NNNN_*.sql migrations.",
    )
    args = parser.parse_args()

    root = Path(args.migrations_dir)
    if not root.is_dir():
        print(f"migrations dir not found: {root}")
        return 1

    files = sorted(p for p in root.glob("*.sql"))
    problems: list[str] = []
    for path in files:
        problems.extend(lint_file(path))

    seen: set[int] = set()
    for path in files:
        match = re.match(r"^(\d{4})_", path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in seen:
            problems.append(f"{path.name}: duplicate migration version {version}")
        seen.add(version)

    if not files:
        print("lint_migrations: no migration files found (nothing to check)")
        return 0

    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    print(f"lint_migrations: OK ({len(files)} file(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
