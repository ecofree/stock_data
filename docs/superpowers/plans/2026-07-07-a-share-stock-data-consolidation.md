# A-share Stock Data Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate `D:\accio\stock_data` and `D:\accio\A-share\kpl-qds` into one usable professional A-share short-term trading assistant, then make `D:\accio\A-share\kpl-qds` safe to delete.

**Architecture:** Keep `D:\accio\stock_data` as the only surviving project because it already has the verified KPL API ingestion path, DuckDB schema, normalized views, explainable signals, four-stage candidates, reports, and static web inspection page. Treat `D:\accio\A-share\kpl-qds` as a legacy module library and historical data source: import useful historical tables under `legacy_qds_*`, port selected engines behind adapters, and discard direct QMT execution paths.

**Tech Stack:** Python, DuckDB, pytest, standard-library HTML generation, optional FastAPI/Streamlit only after data and signal integration are stable.

---

## Final Ownership Decision

**Keep:** `D:\accio\stock_data`

**Retire after cutover:** `D:\accio\A-share\kpl-qds`

**Reasons:**
- `stock_data` currently has fresh verification evidence: `32 passed`, real KPL API ingestion, normalized views, signal tables, reports, and `reports\trading_dashboard_latest.html`.
- `stock_data` follows the user's current goal: no automatic order execution, service for pre-market, auction, intraday monitoring, close decision, review, backtest, and risk constraints.
- `A-share\kpl-qds` has valuable code assets, but its own audit documents show many engines are written but not connected to scheduler/dashboard. It also has a different schema and a hardcoded API key in `config\settings.yaml`.
- Keeping `stock_data` avoids splitting future work across two incompatible DuckDB schemas.

## What Moves From `A-share\kpl-qds`

**Move as data import:**
- `db\kpl_qds.duckdb` historical tables with rows: `daily_sentiment`, `daily_watchlist`, `sector_strength`, `ladder_stocks`, `limit_up_stocks`, `broken_stocks`, `lhb_detail`, `stock_capital_flow`, `sector_capital_flow`, `yesterday_limitup_detail`, `intraday_signals`, `trade_log`.

**Port as code concepts:**
- `engine\backtester.py` -> professional backtest semantics: T+1, fees, slippage, no lookahead.
- `engine\risk_enforcer.py` -> operator risk checks, but no QMT order submission.
- `engine\performance_tracker.py` -> candidate/signal performance tracking.
- `engine\auction_analyzer.py` -> deep auction analyzer, degraded gracefully when `auction_tick` is empty.
- `designs\kpl-qds-dashboard\Dashboard.html` -> visual reference for the future integrated dashboard.

**Do not move into the surviving runtime:**
- `engine\qmt_bridge.py` real/semi-auto execution path.
- `qlib_ext\*` model training path until the data layer and deterministic backtest are stable.
- `data\qlib_data\features\*.bin`; these are bulky generated artifacts.
- `config\settings.yaml` secrets.

---

## New File Structure

### Create
- `D:\accio\stock_data\trade_system\integration\__init__.py`  
  Integration package marker.

- `D:\accio\stock_data\trade_system\integration\legacy_a_share.py`  
  Audits and imports selected `A-share\kpl-qds` DuckDB tables into `stock_data` under `legacy_qds_*`.

- `D:\accio\stock_data\trade_system\integration\operator_views.py`  
  Builds unified operator-facing views over native `stock_data` tables plus imported legacy tables.

- `D:\accio\stock_data\trade_system\operator_risk.py`  
  Ported, simplified risk enforcer for trade plans and watchlist decisions. No order submission.

- `D:\accio\stock_data\trade_system\operator_backtest.py`  
  Professional backtest runner for staged candidates with T+1, fees, and slippage.

- `D:\accio\stock_data\trade_system\operator_performance.py`  
  Performance tracker for candidate stages, alerts, and operator decisions.

- `D:\accio\stock_data\trade_system\auction_deep.py`  
  Deep auction analyzer adapter. Uses `auction_tick` when available; uses `auction_bidding_anomaly` as degraded input when ticks are empty.

- `D:\accio\stock_data\scripts\audit_a_share_project.py`  
  Read-only audit command for `D:\accio\A-share\kpl-qds`.

- `D:\accio\stock_data\scripts\import_legacy_a_share.py`  
  One-way import command from legacy DuckDB into the surviving `stock_data` DuckDB.

- `D:\accio\stock_data\scripts\build_operator_views.py`  
  Builds unified views after import.

- `D:\accio\stock_data\scripts\run_integrated_daily.py`  
  One command for the surviving daily workflow: repair, dedupe, views, signals, reports, dashboard.

- `D:\accio\stock_data\reports\integration_audit_latest.md`  
  Generated audit report.

- `D:\accio\stock_data\reports\legacy_import_latest.md`  
  Generated import report.

- `D:\accio\stock_data\docs\retirement\a_share_delete_checklist.md`  
  Deletion checklist for the old project.

### Modify
- `D:\accio\stock_data\requirements.txt`  
  Add only dependencies actually needed by surviving code. Do not add Streamlit/FastAPI in the first cutover unless the implementation uses them.

- `D:\accio\stock_data\trade_system\web_report.py`  
  Add imported legacy counts and operator view summaries to the existing static web inspection page.

- `D:\accio\stock_data\tests\test_web_dashboard.py`  
  Add checks that the dashboard surfaces legacy import status and unified operator views.

### Tests To Add
- `D:\accio\stock_data\tests\test_legacy_a_share_integration.py`
- `D:\accio\stock_data\tests\test_operator_views.py`
- `D:\accio\stock_data\tests\test_operator_risk.py`
- `D:\accio\stock_data\tests\test_operator_backtest.py`
- `D:\accio\stock_data\tests\test_auction_deep.py`
- `D:\accio\stock_data\tests\test_integrated_daily_runner.py`

---

## Phase 0: Freeze, Audit, And Decide

**Objective:** Create an evidence file proving what will be kept, imported, ported, or discarded before any destructive action.

**Files:**
- Create: `D:\accio\stock_data\trade_system\integration\legacy_a_share.py`
- Create: `D:\accio\stock_data\scripts\audit_a_share_project.py`
- Create: `D:\accio\stock_data\tests\test_legacy_a_share_integration.py`

### Task 0.1: Write Legacy Audit Tests

- [ ] Add this test file:

```python
# D:\accio\stock_data\tests\test_legacy_a_share_integration.py
import duckdb

from trade_system.integration.legacy_a_share import audit_legacy_project


def test_audit_legacy_project_reports_tables_counts_and_import_plan(tmp_path):
    legacy_root = tmp_path / "legacy"
    db_dir = legacy_root / "db"
    db_dir.mkdir(parents=True)
    legacy_db = db_dir / "kpl_qds.duckdb"
    con = duckdb.connect(str(legacy_db))
    con.execute("CREATE TABLE daily_watchlist(date DATE, stock_code VARCHAR, final_score DOUBLE)")
    con.execute("INSERT INTO daily_watchlist VALUES ('2026-07-05', '000001', 88.5)")
    con.execute("CREATE TABLE auction_snapshots(date DATE, stock_code VARCHAR)")
    con.close()

    result = audit_legacy_project(legacy_root)

    assert result["legacy_root"] == str(legacy_root)
    assert result["database"]["exists"] is True
    assert result["tables"]["daily_watchlist"]["row_count"] == 1
    assert result["tables"]["auction_snapshots"]["row_count"] == 0
    assert "daily_watchlist" in result["import_tables"]
    assert "qmt_bridge.py" in result["discard_files"]
```

- [ ] Run to verify red:

```powershell
$env:PYTHONPATH='C:\Users\coumoo\Documents\Codex\2026-07-07\ji\work\pydeps;D:\accio\stock_data'
$env:PYTHONIOENCODING='utf-8'
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_legacy_a_share_integration.py
```

Expected: fails with `ModuleNotFoundError: No module named 'trade_system.integration'`.

### Task 0.2: Implement Legacy Audit

- [ ] Create `D:\accio\stock_data\trade_system\integration\__init__.py` as an empty file.

- [ ] Create `D:\accio\stock_data\trade_system\integration\legacy_a_share.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


IMPORT_TABLES = [
    "daily_sentiment",
    "daily_watchlist",
    "sector_strength",
    "ladder_stocks",
    "limit_up_stocks",
    "broken_stocks",
    "lhb_detail",
    "stock_capital_flow",
    "sector_capital_flow",
    "yesterday_limitup_detail",
    "intraday_signals",
    "trade_log",
]

PORT_FILES = [
    "engine/backtester.py",
    "engine/risk_enforcer.py",
    "engine/performance_tracker.py",
    "engine/auction_analyzer.py",
    "designs/kpl-qds-dashboard/Dashboard.html",
]

DISCARD_FILES = [
    "engine/qmt_bridge.py",
    "config/settings.yaml",
    "qlib_ext/model_trainer.py",
    "qlib_ext/inference_pipeline.py",
]


def _count_rows(con: duckdb.DuckDBPyConnection, table_name: str) -> int:
    try:
        return int(con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0])
    except Exception:
        return 0


def _date_range(con: duckdb.DuckDBPyConnection, table_name: str) -> dict[str, str | None]:
    try:
        cols = {row[1] for row in con.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
        if "date" not in cols:
            return {"min_date": None, "max_date": None}
        row = con.execute(f'SELECT min(date), max(date) FROM "{table_name}"').fetchone()
        return {"min_date": str(row[0]) if row and row[0] else None, "max_date": str(row[1]) if row and row[1] else None}
    except Exception:
        return {"min_date": None, "max_date": None}


def audit_legacy_project(legacy_root: str | Path) -> dict[str, Any]:
    root = Path(legacy_root)
    db_path = root / "db" / "kpl_qds.duckdb"
    result: dict[str, Any] = {
        "legacy_root": str(root),
        "database": {"path": str(db_path), "exists": db_path.exists(), "size": db_path.stat().st_size if db_path.exists() else 0},
        "tables": {},
        "import_tables": IMPORT_TABLES,
        "port_files": PORT_FILES,
        "discard_files": DISCARD_FILES,
    }
    if not db_path.exists():
        return result
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        table_names = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
            ).fetchall()
        ]
        for name in table_names:
            result["tables"][name] = {"row_count": _count_rows(con, name), **_date_range(con, name)}
    finally:
        con.close()
    return result
```

- [ ] Add `D:\accio\stock_data\scripts\audit_a_share_project.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.legacy_a_share import audit_legacy_project


def render_markdown(result: dict) -> str:
    lines = [
        "# A-share Legacy Project Audit",
        "",
        f"- Legacy root: `{result['legacy_root']}`",
        f"- Database exists: `{result['database']['exists']}`",
        f"- Database size: `{result['database']['size']}` bytes",
        "",
        "## Tables",
        "",
        "| Table | Rows | Min Date | Max Date |",
        "|---|---:|---|---|",
    ]
    for name, item in sorted(result.get("tables", {}).items()):
        lines.append(f"| {name} | {item['row_count']} | {item['min_date'] or ''} | {item['max_date'] or ''} |")
    lines.extend(["", "## Import Tables", "", ", ".join(result["import_tables"]), "", "## Port Files", "", ", ".join(result["port_files"]), ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit legacy A-share kpl-qds project before consolidation.")
    parser.add_argument("--legacy-root", default=r"D:\accio\A-share\kpl-qds")
    parser.add_argument("--out", default="reports/integration_audit_latest.md")
    args = parser.parse_args()
    result = audit_legacy_project(args.legacy_root)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(result), encoding="utf-8")
    print(f"integration_audit={out}")
    print(f"database_exists={result['database']['exists']}")
    print(f"table_count={len(result.get('tables', {}))}")
    return 0 if result["database"]["exists"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Run green:

```powershell
$env:PYTHONPATH='C:\Users\coumoo\Documents\Codex\2026-07-07\ji\work\pydeps;D:\accio\stock_data'
$env:PYTHONIOENCODING='utf-8'
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_legacy_a_share_integration.py
```

Expected: `1 passed`.

- [ ] Run real audit:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\audit_a_share_project.py --legacy-root D:\accio\A-share\kpl-qds --out reports\integration_audit_latest.md
```

Expected:
- `database_exists=True`
- `table_count=18`
- `reports\integration_audit_latest.md` exists and is non-empty.

---

## Phase 1: Import Legacy Data Into The Surviving Project

**Objective:** Copy useful legacy rows into `stock_data` without changing or deleting legacy files.

**Files:**
- Modify: `D:\accio\stock_data\trade_system\integration\legacy_a_share.py`
- Create: `D:\accio\stock_data\scripts\import_legacy_a_share.py`
- Modify: `D:\accio\stock_data\tests\test_legacy_a_share_integration.py`

### Task 1.1: Add Import Tests

- [ ] Append this test:

```python
def test_import_legacy_tables_copies_selected_tables_with_prefix(tmp_path):
    stock_db = tmp_path / "stock.duckdb"
    legacy_root = tmp_path / "legacy"
    db_dir = legacy_root / "db"
    db_dir.mkdir(parents=True)
    legacy_db = db_dir / "kpl_qds.duckdb"
    con = duckdb.connect(str(legacy_db))
    con.execute("CREATE TABLE daily_watchlist(date DATE, stock_code VARCHAR, final_score DOUBLE)")
    con.execute("INSERT INTO daily_watchlist VALUES ('2026-07-05', '000001', 88.5)")
    con.execute("CREATE TABLE sector_strength(date DATE, sector_name VARCHAR, strength_score DOUBLE)")
    con.execute("INSERT INTO sector_strength VALUES ('2026-07-05', '测试板块', 77.0)")
    con.close()

    from trade_system.integration.legacy_a_share import import_legacy_tables

    result = import_legacy_tables(stock_db, legacy_root)

    assert result["legacy_qds_daily_watchlist"] == 1
    assert result["legacy_qds_sector_strength"] == 1
    con = duckdb.connect(str(stock_db))
    row = con.execute("SELECT stock_code, final_score, legacy_source_table FROM legacy_qds_daily_watchlist").fetchone()
    con.close()
    assert row == ("000001", 88.5, "daily_watchlist")
```

- [ ] Run red:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_legacy_a_share_integration.py::test_import_legacy_tables_copies_selected_tables_with_prefix
```

Expected: fails because `import_legacy_tables` does not exist.

### Task 1.2: Implement Import

- [ ] Add this function to `legacy_a_share.py`:

```python
def import_legacy_tables(stock_db_path: str | Path, legacy_root: str | Path, tables: list[str] | None = None) -> dict[str, int]:
    root = Path(legacy_root)
    legacy_db = root / "db" / "kpl_qds.duckdb"
    selected = tables or IMPORT_TABLES
    if not legacy_db.exists():
        raise FileNotFoundError(str(legacy_db))
    con = duckdb.connect(str(stock_db_path))
    copied: dict[str, int] = {}
    try:
        con.execute("ATTACH ? AS legacy_db (READ_ONLY)", [str(legacy_db)])
        existing = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM legacy_db.information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        for table in selected:
            target = f"legacy_qds_{table}"
            if table not in existing:
                copied[target] = 0
                continue
            con.execute(f'DROP TABLE IF EXISTS "{target}"')
            con.execute(
                f'''
                CREATE TABLE "{target}" AS
                SELECT *, '{table}' AS legacy_source_table, current_timestamp AS legacy_imported_at
                FROM legacy_db.main."{table}"
                '''
            )
            copied[target] = int(con.execute(f'SELECT count(*) FROM "{target}"').fetchone()[0])
        con.execute("DETACH legacy_db")
    finally:
        con.close()
    return copied
```

- [ ] Create `D:\accio\stock_data\scripts\import_legacy_a_share.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.legacy_a_share import import_legacy_tables


def main() -> int:
    parser = argparse.ArgumentParser(description="Import legacy A-share kpl-qds tables into stock_data DuckDB.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--legacy-root", default=r"D:\accio\A-share\kpl-qds")
    parser.add_argument("--out", default="reports/legacy_import_latest.md")
    args = parser.parse_args()
    result = import_legacy_tables(args.db, args.legacy_root)
    lines = ["# Legacy A-share Import", "", "| Table | Rows |", "|---|---:|"]
    for name, count in sorted(result.items()):
        lines.append(f"| {name} | {count} |")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"legacy_import={out}")
    for name, count in sorted(result.items()):
        print(f"{name}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Run green:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_legacy_a_share_integration.py
```

Expected: all tests in this file pass.

- [ ] Run real import:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\import_legacy_a_share.py --db kpl_data.duckdb --legacy-root D:\accio\A-share\kpl-qds --out reports\legacy_import_latest.md
```

Expected imported row counts include:
- `legacy_qds_daily_watchlist` > 0
- `legacy_qds_sector_strength` > 0
- `legacy_qds_limit_up_stocks` > 0
- `legacy_qds_lhb_detail` > 0

---

## Phase 2: Build Unified Operator Views

**Objective:** Make the surviving project read native and legacy data through one operator-facing schema.

**Files:**
- Create: `D:\accio\stock_data\trade_system\integration\operator_views.py`
- Create: `D:\accio\stock_data\scripts\build_operator_views.py`
- Create: `D:\accio\stock_data\tests\test_operator_views.py`

### Task 2.1: Add Operator View Tests

- [ ] Create test file:

```python
import duckdb

from trade_system.integration.operator_views import build_operator_views


def test_operator_views_prefer_native_signals_and_include_legacy(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE stock_candidate_stage_signal(trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)")
    con.execute("INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-06', 'premarket_pool', '000001', 'native stock', 91, 'watch', '{}', '2026-07-06 15:00:00')")
    con.execute("CREATE TABLE legacy_qds_daily_watchlist(date DATE, stock_code VARCHAR, stock_name VARCHAR, final_score DOUBLE, legacy_source_table VARCHAR, legacy_imported_at TIMESTAMP)")
    con.execute("INSERT INTO legacy_qds_daily_watchlist VALUES ('2026-07-05', '000002', 'legacy stock', 80, 'daily_watchlist', '2026-07-07 12:00:00')")
    con.close()

    build_operator_views(db_path)

    con = duckdb.connect(str(db_path))
    rows = con.execute("SELECT trade_date, stage, stock_code, data_origin FROM v_operator_candidates ORDER BY trade_date, stock_code").fetchall()
    con.close()
    assert rows == [
        ("2026-07-05", "legacy_watchlist", "000002", "legacy_qds"),
        ("2026-07-06", "premarket_pool", "000001", "stock_data"),
    ]
```

- [ ] Run red:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_operator_views.py
```

Expected: fails because module is missing.

### Task 2.2: Implement Views

- [ ] Create `operator_views.py`:

```python
from __future__ import annotations

from pathlib import Path

import duckdb


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name = ?",
        [name],
    ).fetchone()
    if row and row[0]:
        return True
    row = con.execute(
        "SELECT count(*) FROM information_schema.views WHERE table_schema='main' AND table_name = ?",
        [name],
    ).fetchone()
    return bool(row and row[0])


def _empty_candidates_sql() -> str:
    return """
    SELECT
        CAST(NULL AS VARCHAR) AS trade_date,
        CAST(NULL AS VARCHAR) AS stage,
        CAST(NULL AS VARCHAR) AS stock_code,
        CAST(NULL AS VARCHAR) AS stock_name,
        CAST(NULL AS DOUBLE) AS score,
        CAST(NULL AS VARCHAR) AS decision,
        CAST(NULL AS VARCHAR) AS data_origin
    WHERE false
    """


def build_operator_views(db_path: str | Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        native = (
            """
            SELECT trade_date, stage, stock_code, stock_name, score, decision, 'stock_data' AS data_origin
            FROM stock_candidate_stage_signal
            """
            if _relation_exists(con, "stock_candidate_stage_signal")
            else _empty_candidates_sql()
        )
        legacy = (
            """
            SELECT
                CAST(date AS VARCHAR) AS trade_date,
                'legacy_watchlist' AS stage,
                stock_code,
                stock_name,
                final_score AS score,
                'legacy_reference' AS decision,
                'legacy_qds' AS data_origin
            FROM legacy_qds_daily_watchlist
            """
            if _relation_exists(con, "legacy_qds_daily_watchlist")
            else _empty_candidates_sql()
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_operator_candidates AS
            SELECT * FROM ({native})
            UNION ALL
            SELECT * FROM ({legacy})
            """
        )
    finally:
        con.close()
```

- [ ] Add `scripts\build_operator_views.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.integration.operator_views import build_operator_views


def main() -> int:
    parser = argparse.ArgumentParser(description="Build unified operator views.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    build_operator_views(args.db)
    print("Built operator views: v_operator_candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Run green:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_operator_views.py
```

Expected: `1 passed`.

- [ ] Run real view build:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\build_operator_views.py --db kpl_data.duckdb
```

- [ ] Verify real view:

```powershell
@'
import duckdb
con=duckdb.connect('kpl_data.duckdb', read_only=True)
print(con.execute("SELECT data_origin, stage, count(*) FROM v_operator_candidates GROUP BY 1,2 ORDER BY 1,2").fetchall())
con.close()
'@ | & 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -
```

Expected: both `stock_data` and `legacy_qds` rows appear if import has run.

---

## Phase 3: Consolidate Reports And Web Inspection Page

**Objective:** Make the surviving web page show native + legacy integrated status, so the user can inspect one project only.

**Files:**
- Modify: `D:\accio\stock_data\trade_system\web_report.py`
- Modify: `D:\accio\stock_data\tests\test_web_dashboard.py`
- Modify: `D:\accio\stock_data\scripts\generate_web_dashboard.py`

### Task 3.1: Add Dashboard Legacy View Tests

- [ ] Add to `tests\test_web_dashboard.py`:

```python
def test_dashboard_context_includes_operator_candidate_origin(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    con = duckdb.connect(str(db_path))
    con.execute("CREATE VIEW v_operator_candidates AS SELECT '2026-07-06' AS trade_date, 'premarket_pool' AS stage, '000001' AS stock_code, 'native' AS stock_name, 90.0 AS score, 'watch' AS decision, 'stock_data' AS data_origin")
    con.close()

    context = load_dashboard_context(str(db_path), reports_dir)

    assert context["operator_candidates"][0]["data_origin"] == "stock_data"
```

- [ ] Run red:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_web_dashboard.py::test_dashboard_context_includes_operator_candidate_origin
```

Expected: fails with missing `operator_candidates`.

### Task 3.2: Implement Dashboard Integration

- [ ] In `web_report.py`, add `operator_candidates` to `load_dashboard_context`:

```python
operator_candidates = _latest_rows(con, "v_operator_candidates", "score", 30)
```

- [ ] Add it to the returned context:

```python
"operator_candidates": operator_candidates,
```

- [ ] In `render_dashboard_html`, add a section after the current candidate section:

```python
operator_rows = "\n".join(
    f"<tr><td>{idx}</td><td>{_fmt(item.get('stock_name') or item.get('stock_code'))}</td><td>{_fmt(item.get('stage'))}</td><td>{_fmt(item.get('score'))}</td><td>{_fmt(item.get('data_origin'))}</td></tr>"
    for idx, item in enumerate(context.get("operator_candidates", [])[:30], start=1)
)
```

and add HTML:

```html
<section>
  <h2>整合候选池</h2>
  <table><thead><tr><th>Rank</th><th>名称</th><th>阶段</th><th>分数</th><th>来源</th></tr></thead><tbody>{operator_rows}</tbody></table>
</section>
```

- [ ] Run tests:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_web_dashboard.py
```

Expected: all dashboard tests pass.

- [ ] Generate real dashboard:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\generate_web_dashboard.py --db kpl_data.duckdb --out reports\trading_dashboard_latest.html
```

Expected: page includes `整合候选池`.

---

## Phase 4: Port Professional Risk Without QMT Execution

**Objective:** Bring the useful risk checks from `A-share\kpl-qds` into `stock_data` while preserving the no-auto-ordering requirement.

**Files:**
- Create: `D:\accio\stock_data\trade_system\operator_risk.py`
- Create: `D:\accio\stock_data\tests\test_operator_risk.py`

### Task 4.1: Add Risk Tests

- [ ] Create test:

```python
from trade_system.operator_risk import OperatorRiskConfig, TradePlanInput, evaluate_trade_plan


def test_evaluate_trade_plan_blocks_when_market_position_limit_is_exceeded():
    config = OperatorRiskConfig(max_total_position_pct=20, max_single_stock_pct=10, min_score=70)
    plan = TradePlanInput(stock_code="000001", score=85, planned_position_pct=15, current_total_position_pct=10, market_regime="退潮")

    result = evaluate_trade_plan(plan, config)

    assert result.allowed is False
    assert result.severity == "P0"
    assert "total position" in result.reason


def test_evaluate_trade_plan_allows_small_high_score_plan():
    config = OperatorRiskConfig(max_total_position_pct=20, max_single_stock_pct=10, min_score=70)
    plan = TradePlanInput(stock_code="000001", score=85, planned_position_pct=5, current_total_position_pct=5, market_regime="启动")

    result = evaluate_trade_plan(plan, config)

    assert result.allowed is True
    assert result.severity == "OK"
```

- [ ] Run red:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_operator_risk.py
```

Expected: module missing.

### Task 4.2: Implement Risk Module

- [ ] Create `operator_risk.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OperatorRiskConfig:
    max_total_position_pct: float = 20.0
    max_single_stock_pct: float = 10.0
    min_score: float = 70.0


@dataclass(frozen=True)
class TradePlanInput:
    stock_code: str
    score: float
    planned_position_pct: float
    current_total_position_pct: float
    market_regime: str


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    severity: str
    reason: str


def evaluate_trade_plan(plan: TradePlanInput, config: OperatorRiskConfig | None = None) -> RiskDecision:
    cfg = config or OperatorRiskConfig()
    if plan.current_total_position_pct + plan.planned_position_pct > cfg.max_total_position_pct:
        return RiskDecision(False, "P0", "total position limit exceeded")
    if plan.planned_position_pct > cfg.max_single_stock_pct:
        return RiskDecision(False, "P0", "single stock position limit exceeded")
    if plan.score < cfg.min_score:
        return RiskDecision(False, "P1", "candidate score below minimum")
    if plan.market_regime in {"退潮", "冰点"} and plan.planned_position_pct > cfg.max_single_stock_pct / 2:
        return RiskDecision(False, "P1", "weak market regime requires reduced probing position")
    return RiskDecision(True, "OK", "plan passed operator risk checks")
```

- [ ] Run green:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_operator_risk.py
```

Expected: `2 passed`.

---

## Phase 5: Port Professional Backtest Semantics

**Objective:** Upgrade staged candidate backtest with explicit T+1 entry/exit, fees, and slippage while keeping the existing lightweight backtest for comparison.

**Files:**
- Create: `D:\accio\stock_data\trade_system\operator_backtest.py`
- Create: `D:\accio\stock_data\tests\test_operator_backtest.py`
- Create: `D:\accio\stock_data\scripts\run_operator_backtest.py`

### Task 5.1: Add Backtest Tests

- [ ] Create test:

```python
import duckdb

from trade_system.operator_backtest import run_operator_stage_backtest


def test_operator_stage_backtest_applies_t1_fees_and_slippage(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE stock_candidate_stage_signal(trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, decision VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP)")
    con.execute("INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-06', 'premarket_pool', '000001', 'test stock', 90, 'watch', '{}', '2026-07-06')")
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO kline VALUES ('2026-07-06', '000001', 10, 10, 10, 10, 1000, 10000, 0, 'D', '2026-07-06')")
    con.execute("INSERT INTO kline VALUES ('2026-07-07', '000001', 10.0, 11.0, 9.8, 10.5, 1000, 10500, 5, 'D', '2026-07-07')")
    con.close()

    result = run_operator_stage_backtest(db_path, fee_rate=0.001, slippage_bps=10)

    assert result["sample_count"] == 1
    row = result["rows"][0]
    assert row["entry_date"] == "2026-07-07"
    assert row["gross_return_pct"] == 5.0
    assert row["net_return_pct"] < 5.0
```

- [ ] Run red:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_operator_backtest.py
```

Expected: module missing.

### Task 5.2: Implement Operator Backtest

- [ ] Create `operator_backtest.py` with deterministic T+1 close-to-close semantics:

```python
from __future__ import annotations

from pathlib import Path

import duckdb


def run_operator_stage_backtest(db_path: str | Path, fee_rate: float = 0.001, slippage_bps: float = 10.0) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        signals = con.execute(
            """
            SELECT trade_date, stage, stock_code, stock_name, score
            FROM stock_candidate_stage_signal
            ORDER BY trade_date, stock_code, stage
            """
        ).fetchall()
        kline = con.execute(
            """
            SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, close
            FROM kline
            WHERE close IS NOT NULL
            ORDER BY stock_code, date
            """
        ).fetchall()
    finally:
        con.close()

    by_stock: dict[str, list[tuple[str, float]]] = {}
    for trade_date, stock_code, close in kline:
        by_stock.setdefault(stock_code, []).append((trade_date, float(close)))

    rows = []
    slippage = slippage_bps / 10000.0
    for trade_date, stage, stock_code, stock_name, score in signals:
        series = by_stock.get(stock_code, [])
        for index, (date_value, close) in enumerate(series):
            if date_value == trade_date and index + 1 < len(series):
                entry_date, exit_close = series[index + 1]
                gross = (exit_close - close) * 100.0 / close
                net = gross - fee_rate * 100.0 - slippage * 100.0
                rows.append(
                    {
                        "signal_date": trade_date,
                        "entry_date": entry_date,
                        "stage": stage,
                        "stock_code": stock_code,
                        "stock_name": stock_name,
                        "score": score,
                        "gross_return_pct": round(gross, 2),
                        "net_return_pct": round(net, 2),
                    }
                )
                break
    return {"sample_count": len(rows), "rows": rows}
```

- [ ] Create `scripts\run_operator_backtest.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.operator_backtest import run_operator_stage_backtest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run professional operator staged backtest.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    result = run_operator_stage_backtest(args.db)
    print(f"operator_backtest_samples={result['sample_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Run green:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_operator_backtest.py
```

Expected: `1 passed`.

---

## Phase 6: Deep Auction Adapter

**Objective:** Use the concept from `A-share\kpl-qds\engine\auction_analyzer.py`, but make it work with current KPL data quality: real tick rows if present, anomaly-only degraded status if ticks are empty.

**Files:**
- Create: `D:\accio\stock_data\trade_system\auction_deep.py`
- Create: `D:\accio\stock_data\tests\test_auction_deep.py`

### Task 6.1: Add Auction Tests

- [ ] Create test:

```python
import duckdb

from trade_system.auction_deep import analyze_auction_deep


def test_auction_deep_reports_degraded_when_tick_is_empty_but_anomaly_exists(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE auction_bidding_anomaly(date DATE, stock_code VARCHAR, anomaly_type VARCHAR, anomaly_value DOUBLE, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO auction_bidding_anomaly VALUES ('2026-07-06', '000001', 'bidding_amount', 100000000, '2026-07-06 09:25:00')")
    con.close()

    result = analyze_auction_deep(db_path, "2026-07-06")

    assert result["mode"] == "degraded_anomaly"
    assert result["rows"][0]["stock_code"] == "000001"
    assert result["rows"][0]["reliability"] == "medium"
```

- [ ] Run red:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_auction_deep.py
```

Expected: module missing.

### Task 6.2: Implement Degraded Auction Analyzer

- [ ] Create `auction_deep.py`:

```python
from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.quality import table_exists


def analyze_auction_deep(db_path: str | Path, trade_date: str) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tick_count = 0
        if table_exists(con, "auction_tick"):
            tick_count = con.execute(
                "SELECT count(*) FROM auction_tick WHERE CAST(date AS VARCHAR)=?",
                [trade_date],
            ).fetchone()[0]
        if tick_count:
            return {"mode": "tick", "rows": []}
        if not table_exists(con, "auction_bidding_anomaly"):
            return {"mode": "missing", "rows": []}
        rows = con.execute(
            """
            SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, anomaly_type, anomaly_value
            FROM auction_bidding_anomaly
            WHERE CAST(date AS VARCHAR)=?
            ORDER BY anomaly_value DESC NULLS LAST
            """,
            [trade_date],
        ).fetchall()
    finally:
        con.close()
    return {
        "mode": "degraded_anomaly" if rows else "missing",
        "rows": [
            {
                "trade_date": trade_date,
                "stock_code": stock_code,
                "anomaly_type": anomaly_type,
                "anomaly_value": float(anomaly_value or 0),
                "reliability": "medium",
                "operator_note": "竞价逐笔为空，使用竞价异常做降级确认。",
            }
            for _, stock_code, anomaly_type, anomaly_value in rows
        ],
    }
```

- [ ] Run green:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_auction_deep.py
```

Expected: `1 passed`.

---

## Phase 7: One Surviving Daily Command

**Objective:** Replace scattered commands with one integrated daily command in `stock_data`.

**Files:**
- Create: `D:\accio\stock_data\scripts\run_integrated_daily.py`
- Create: `D:\accio\stock_data\tests\test_integrated_daily_runner.py`

### Task 7.1: Add Runner Test

- [ ] Create a test for command composition:

```python
from scripts.run_integrated_daily import command_plan


def test_integrated_daily_command_plan_contains_required_steps():
    steps = command_plan("kpl_data.duckdb")
    names = [step[0] for step in steps]
    assert names == [
        "repair_kline_raw_json",
        "repair_duplicates",
        "build_normalized_views",
        "build_operator_views",
        "generate_signals",
        "run_stage_backtest",
        "run_operator_backtest",
        "audit_data_quality",
        "assess_data_chains",
        "generate_professional_reports",
        "generate_web_dashboard",
    ]
```

- [ ] Run red:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_integrated_daily_runner.py
```

Expected: module missing.

### Task 7.2: Implement Runner

- [ ] Create `scripts\run_integrated_daily.py`:

```python
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def command_plan(db_path: str) -> list[tuple[str, list[str]]]:
    py = sys.executable
    return [
        ("repair_kline_raw_json", [py, "scripts/repair_kline_raw_json.py", "--db", db_path]),
        ("repair_duplicates", [py, "scripts/repair_duplicates.py", "--db", db_path]),
        ("build_normalized_views", [py, "scripts/build_normalized_views.py", "--db", db_path]),
        ("build_operator_views", [py, "scripts/build_operator_views.py", "--db", db_path]),
        ("generate_signals", [py, "scripts/generate_signals.py", "--db", db_path]),
        ("run_stage_backtest", [py, "scripts/run_stage_backtest.py", "--db", db_path, "--out", "reports/stage_backtest_latest.md"]),
        ("run_operator_backtest", [py, "scripts/run_operator_backtest.py", "--db", db_path]),
        ("audit_data_quality", [py, "scripts/audit_data_quality.py", "--db", db_path, "--schema", "schema.py", "--out", "reports/data_quality_latest.md"]),
        ("assess_data_chains", [py, "scripts/assess_data_chains.py", "--db", db_path, "--out", "reports/data_chain_status_latest.md"]),
        ("generate_professional_reports", [py, "scripts/generate_professional_reports.py", "--db", db_path, "--out-dir", "reports"]),
        ("generate_web_dashboard", [py, "scripts/generate_web_dashboard.py", "--db", db_path, "--out", "reports/trading_dashboard_latest.html"]),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the integrated surviving stock_data daily workflow.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for name, cmd in command_plan(args.db):
        print(f"RUN {name}: {' '.join(cmd)}")
        if not args.dry_run:
            subprocess.run(cmd, cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Run green:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests\test_integrated_daily_runner.py
```

Expected: `1 passed`.

- [ ] Run dry run:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\run_integrated_daily.py --db kpl_data.duckdb --dry-run
```

Expected: all 11 step names print in order.

- [ ] Run real integrated workflow:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\run_integrated_daily.py --db kpl_data.duckdb
```

Expected:
- exits `0`
- `reports\trading_dashboard_latest.html` updates
- all reports remain non-empty.

---

## Phase 8: Cutover Verification And Retirement

**Objective:** Prove the surviving project is complete enough, then delete the old project manually or with an explicit approved command.

**Files:**
- Create: `D:\accio\stock_data\docs\retirement\a_share_delete_checklist.md`

### Task 8.1: Create Deletion Checklist

- [ ] Create:

```markdown
# A-share Legacy Project Deletion Checklist

Legacy project: `D:\accio\A-share\kpl-qds`
Surviving project: `D:\accio\stock_data`

Delete the legacy project only after all checks are true:

- [ ] `scripts\audit_a_share_project.py` has generated `reports\integration_audit_latest.md`.
- [ ] `scripts\import_legacy_a_share.py` has generated `reports\legacy_import_latest.md`.
- [ ] `legacy_qds_daily_watchlist`, `legacy_qds_sector_strength`, `legacy_qds_limit_up_stocks`, and `legacy_qds_lhb_detail` exist in `kpl_data.duckdb`.
- [ ] `v_operator_candidates` contains both `stock_data` and `legacy_qds` rows.
- [ ] `scripts\run_integrated_daily.py --db kpl_data.duckdb` exits with code `0`.
- [ ] `pytest -q` passes.
- [ ] `reports\trading_dashboard_latest.html` opens and shows integrated candidates.
- [ ] No required API key exists only in `D:\accio\A-share\kpl-qds\config\settings.yaml`.
- [ ] User has explicitly confirmed deletion.

Recommended deletion method after confirmation:

```powershell
Remove-Item -LiteralPath 'D:\accio\A-share\kpl-qds' -Recurse
```
```

### Task 8.2: Final Verification Commands

- [ ] Run all tests:

```powershell
$env:PYTHONPATH='C:\Users\coumoo\Documents\Codex\2026-07-07\ji\work\pydeps;D:\accio\stock_data'
$env:PYTHONIOENCODING='utf-8'
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q
```

Expected: all tests pass.

- [ ] Run database proof:

```powershell
@'
import duckdb
con=duckdb.connect('kpl_data.duckdb', read_only=True)
checks=[
  "SELECT count(*) FROM legacy_qds_daily_watchlist",
  "SELECT data_origin, count(*) FROM v_operator_candidates GROUP BY 1 ORDER BY 1",
  "SELECT count(*) FROM stock_candidate_stage_signal",
  "SELECT source_table, is_fallback, count(*) FROM v_index_state GROUP BY 1,2",
]
for sql in checks:
    print(sql)
    print(con.execute(sql).fetchall())
con.close()
'@ | & 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -
```

Expected:
- legacy row counts are greater than zero
- `v_operator_candidates` includes `legacy_qds`
- native stage signals still exist
- index state remains non-fallback if `l2_realtime_index_list` exists.

---

## Acceptance Criteria

The consolidation is complete when:

- `D:\accio\stock_data` contains all active runtime code.
- `D:\accio\stock_data\kpl_data.duckdb` contains imported `legacy_qds_*` tables.
- The integrated dashboard is generated at `D:\accio\stock_data\reports\trading_dashboard_latest.html`.
- The one-command daily workflow works:

```powershell
& 'C:\Users\coumoo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\run_integrated_daily.py --db kpl_data.duckdb
```

- Full tests pass.
- `D:\accio\A-share\kpl-qds` is no longer needed for runtime, reporting, or dashboard inspection.

## Risks And Controls

- **Risk:** Hardcoded API key in `A-share\kpl-qds\config\settings.yaml`.  
  **Control:** Do not copy that file. Use `stock_data\.env` or environment variables.

- **Risk:** Schema mismatch between projects.  
  **Control:** Import under `legacy_qds_*`; never rename legacy rows into native tables without an adapter view.

- **Risk:** Automatic trading code enters surviving project.  
  **Control:** Do not import `qmt_bridge.py`; only port pure risk evaluation logic.

- **Risk:** Big qlib binary feature files bloat surviving project.  
  **Control:** Do not copy `data\qlib_data\features\*.bin`.

- **Risk:** Legacy code has dependencies not installed in current environment.  
  **Control:** Port small pure-Python modules behind tests; do not vendor the whole dependency stack.

## Recommended Execution Order

1. Phase 0 audit.
2. Phase 1 legacy data import.
3. Phase 2 unified operator views.
4. Phase 3 web page integration.
5. Phase 7 one-command runner.
6. Run full verification.
7. Phase 4 risk module.
8. Phase 5 operator backtest.
9. Phase 6 auction deep adapter.
10. Phase 8 retirement checklist.

This order makes the system usable quickly after Phase 3/7, then improves professional depth without blocking cutover.
