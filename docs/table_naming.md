# Table naming & schema conventions

Applies to **all new tables and columns**.  Existing tables are grandfathered
(renaming hundreds of production tables would break every consumer); the
`legacy_qds_*`, `_dedupe_archive_*` and pinyin-named tables must not be used
as precedents.

## Rules

1. **English semantic snake_case** for tables and columns
   (`intraday_stock_flow`, not `dingpan_zjmm`).  No pinyin abbreviations.
2. **One concept, one table.**  Before creating a table, search
   `information_schema.tables` for an existing surface covering the same
   data (the K-line family alone has five parallel chains).  Extend the
   canonical table instead of adding a sibling.
3. **Every business table carries a date/trade_date column** plus a
   `fetched_at TIMESTAMP DEFAULT current_timestamp` provenance column.
4. **Idempotent writes are mandatory**: either a primary key with
   `ON CONFLICT DO UPDATE`, or `insert_rows(..., replace_on=[...])`.
   Plain appends require an explicit justification comment.
5. **DELETE+INSERT pairs must be wrapped in `store.transaction()`** so a
   crash cannot drop the day's data.
6. **Schema changes to existing databases go through `migrations/NNNN_*.sql`**
   — never edit `init_schema` for incremental changes.  Run
   `python scripts/lint_migrations.py` locally; it is also part of the
   pre-push gate.
7. Operational/audit artifacts (`*_checkpoint`, `collect_log`,
   `_dedupe_archive_*`) use a leading or trailing marker and never mix with
   market data surfaces.

## Banned identifier fragments (enforced by lint_migrations.py)

Pinyin tokens observed in legacy tables: `zhangting dieting fengban gudong
chouma dingpan fengk weipan qiangchou xianhuo zjmm fenbi duidao tuoyadan
gujia bkjj youzi jingjia liutong huanshou`.  New DDL containing them fails
the lint gate.
