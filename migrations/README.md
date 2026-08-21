# Schema migrations

Numbered, forward-only SQL migrations applied once per database by
`trade_system.migrations.apply_pending` (wired into `schema.init_schema`).

- Naming: `NNNN_short_description.sql` (version `1` is reserved).
- Multiple statements per file are allowed; the file runs in one transaction.
- Write defensively (`IF NOT EXISTS` / `IF EXISTS`) — production databases
  exist in several historical shapes.
- Never edit a migration after it has shipped; add a new one instead.

Manual run:

```powershell
D:\anaconda\python.exe -c "import sys; sys.path.insert(0,'.'); import duckdb; from trade_system.migrations import apply_pending; con=duckdb.connect('kpl_data.duckdb'); print(apply_pending(con))"
```

Dry-run (list what would apply):

```powershell
D:\anaconda\python.exe -c "import sys; sys.path.insert(0,'.'); import duckdb; from trade_system.migrations import apply_pending; con=duckdb.connect('kpl_data.duckdb', read_only=True); print(apply_pending(con, dry_run=True))"
```
