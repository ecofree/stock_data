# Ruff lint adoption status

**Enabled since 2026-08-21**: `E9, F63, F7, F82, F401, F841` — enforced by
`pyproject.toml`, `.github/workflows/ci.yml`, and the local pre-push hook
(`scripts/pre_push_check.ps1`).

These rules already paid for themselves: they caught two latent
`NameError`s in `trade_system/stock_data_sources.py` (`datetime` used
without import, silently masked by `_run`'s broad except) and
Python 3.12-only f-string syntax in `review_web.py` that broke the stated
3.11 support.

## Historical notes

- The 89 F401 unused imports were removed with `ruff --fix`; the
  `from base import logger` concern was resolved structurally — files that
  still need `base.py`'s logging side effect keep a real base import, and
  root `collect_*.py` are shims over `collectors.*`.
- The 14 F841 dead assignments were reviewed individually; one
  (`eastmoney_finance.py`) was an exception bookkeeping leftover converted
  to `pass`, and two evidence-schema list literals in `normalize.py` were
  kept as `_unused_cols` documentation.

## Next candidates (not enabled)

- `B` (bugbear), `SIM` (simplify), `C4` (comprehensions) — style-level,
  expect noise; adopt after a quiet week of the current gate.
