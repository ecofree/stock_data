# A Share Trading Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the current KPL collection folder into a dependable A-share short-term/theme/emotion-cycle trading assistant that supports preparation, monitoring, review, backtesting, and risk constraints without automatic order placement.

**Architecture:** Keep the existing single-folder Python/DuckDB project, but add a small `trade_system` package for auditable services. Existing collectors remain the ingestion layer; new scripts add schema/quality checks, normalized views, signal snapshots, risk/watchlist tables, reports, and backtests. Each stage is usable on its own and verified with pytest plus read-only or explicit DuckDB commands.

**Tech Stack:** Python 3.11, DuckDB, pytest, standard-library CLI tools, local Markdown reports.

---

## File Structure

- Create `requirements.txt`: runtime/test dependencies for the local project.
- Create `.env.example`: documented environment variables without secrets.
- Modify `config.py`: load configuration from environment and optional `.env`.
- Modify `base.py`: add safer API-key handling, query encoding, and idempotent insert support.
- Modify `fetch_all.py`: prevent optional finance collector crashes and support health-check behavior.
- Create `trade_system/__init__.py`: package marker.
- Create `trade_system/schema_audit.py`: compare `schema.py` table definitions with DuckDB tables.
- Create `trade_system/quality.py`: duplicate, coverage, freshness, and missing-table checks.
- Create `trade_system/normalize.py`: create deduplicated normalized views.
- Create `trade_system/signals.py`: generate market regime, sector scores, stock candidates, and alerts.
- Create `trade_system/risk.py`: create watchlist, trade plan, portfolio, journal, and risk snapshot tables.
- Create `trade_system/backtest.py`: run lightweight historical checks on current data.
- Create `trade_system/review.py`: render operator-facing Markdown reports.
- Create `scripts/check_schema.py`: CLI for schema audit.
- Create `scripts/audit_data_quality.py`: CLI for data quality audit.
- Create `scripts/build_normalized_views.py`: CLI for normalized views.
- Create `scripts/generate_signals.py`: CLI for signal tables.
- Create `scripts/init_trading_tables.py`: CLI for risk/operator tables.
- Create `scripts/run_backtest.py`: CLI for backtest summary.
- Create `scripts/generate_review_report.py`: CLI for daily report.
- Create tests under `tests/` for config, quality, views, signals, risk, and backtest behavior.

---

### Task 1: Runtime and Configuration Foundation

**Files:**
- Create: `D:/accio/stock_data/requirements.txt`
- Create: `D:/accio/stock_data/.env.example`
- Modify: `D:/accio/stock_data/config.py`
- Test: `D:/accio/stock_data/tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

```python
import importlib
import os
import sys


def reload_config(monkeypatch, tmp_path, env=None, dotenv_text=""):
    monkeypatch.chdir(tmp_path)
    if dotenv_text:
        (tmp_path / ".env").write_text(dotenv_text, encoding="utf-8")
    for key in ["KPL_API_KEY", "KPL_API_BASE", "KPL_DB_PATH", "KPL_REQUEST_DELAY"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("config", None)
    sys.path.insert(0, "D:/accio/stock_data")
    return importlib.import_module("config")


def test_config_prefers_environment_over_dotenv(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        env={"KPL_API_KEY": "env-key", "KPL_API_BASE": "https://example.test/api"},
        dotenv_text="KPL_API_KEY=dotenv-key\nKPL_API_BASE=https://dotenv.test/api\n",
    )
    assert cfg.API_KEY == "env-key"
    assert cfg.API_BASE == "https://example.test/api"


def test_config_reads_dotenv_when_environment_missing(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        dotenv_text="KPL_API_KEY=dotenv-key\nKPL_REQUEST_DELAY=0.15\n",
    )
    assert cfg.API_KEY == "dotenv-key"
    assert cfg.REQUEST_DELAY == 0.15
```

- [ ] **Step 2: Run config tests and verify RED**

Run: `python -m pytest tests/test_config.py -q`

Expected: FAIL because `tests/test_config.py` or new config helpers do not exist.

- [ ] **Step 3: Implement config foundation**

Create `requirements.txt` with `duckdb` and `pytest`. Create `.env.example` with `KPL_API_KEY=replace-me`. Update `config.py` to read `.env` first, then environment variables, with environment taking precedence.

- [ ] **Step 4: Run config tests and verify GREEN**

Run: `python -m pytest tests/test_config.py -q`

Expected: PASS.

---

### Task 2: Schema and Data Quality Audit

**Files:**
- Create: `D:/accio/stock_data/trade_system/schema_audit.py`
- Create: `D:/accio/stock_data/trade_system/quality.py`
- Create: `D:/accio/stock_data/scripts/check_schema.py`
- Create: `D:/accio/stock_data/scripts/audit_data_quality.py`
- Test: `D:/accio/stock_data/tests/test_schema_quality.py`

- [ ] **Step 1: Write failing audit tests**

```python
import duckdb
from trade_system.schema_audit import parse_defined_tables, audit_schema
from trade_system.quality import find_duplicate_keys


def test_parse_defined_tables_extracts_create_table_names():
    text = "CREATE TABLE IF NOT EXISTS market_mood (date DATE); CREATE TABLE IF NOT EXISTS sector_capital (date DATE);"
    assert parse_defined_tables(text) == ["market_mood", "sector_capital"]


def test_audit_schema_reports_missing_and_extra_tables(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE market_mood(date DATE)")
    con.execute("CREATE TABLE extra_table(id INTEGER)")
    con.close()
    result = audit_schema(str(db_path), ["market_mood", "sector_capital"])
    assert result["missing_defined"] == ["sector_capital"]
    assert result["extra_actual"] == ["extra_table"]


def test_find_duplicate_keys_counts_duplicate_groups(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE market_rise_fall(date DATE, value INTEGER)")
    con.execute("INSERT INTO market_rise_fall VALUES ('2026-07-06', 1), ('2026-07-06', 2), ('2026-07-05', 1)")
    con.close()
    result = find_duplicate_keys(str(db_path), "market_rise_fall", ["date"])
    assert result["duplicate_groups"] == 1
    assert result["max_duplicate_count"] == 2
```

- [ ] **Step 2: Run audit tests and verify RED**

Run: `python -m pytest tests/test_schema_quality.py -q`

Expected: FAIL because audit modules do not exist.

- [ ] **Step 3: Implement audit modules and CLIs**

Implement table-definition parsing, actual-table comparison, row/date coverage, duplicate-key checks, and Markdown report generation to `reports/data_quality_<date>.md`.

- [ ] **Step 4: Run audit tests and verify GREEN**

Run: `python -m pytest tests/test_schema_quality.py -q`

Expected: PASS.

- [ ] **Step 5: Verify against current database**

Run: `python scripts/check_schema.py --db kpl_data.duckdb --schema schema.py`

Expected: exit 0 with a summary that includes missing defined tables rather than crashing.

---

### Task 3: Idempotent Storage and Safe Collection Entry

**Files:**
- Modify: `D:/accio/stock_data/base.py`
- Modify: `D:/accio/stock_data/fetch_all.py`
- Test: `D:/accio/stock_data/tests/test_storage_fetch.py`

- [ ] **Step 1: Write failing storage and fetch tests**

```python
import duckdb
from base import DuckDBStore
from fetch_all import should_collect_finance


def test_insert_rows_can_replace_existing_key_rows(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    store = DuckDBStore(str(db_path))
    store.insert_rows("market_mood", [("2026-07-06", 1)], ["date", "rise_count"], replace_on=["date"])
    store.insert_rows("market_mood", [("2026-07-06", 2)], ["date", "rise_count"], replace_on=["date"])
    rows = store.fetchall("SELECT date, rise_count FROM market_mood")
    store.close()
    assert rows == [("2026-07-06", "2")]


def test_should_collect_finance_requires_collector_and_flag():
    assert should_collect_finance(skip_finance=False, only_market=False, has_finance=True) is True
    assert should_collect_finance(skip_finance=False, only_market=False, has_finance=False) is False
    assert should_collect_finance(skip_finance=True, only_market=False, has_finance=True) is False
    assert should_collect_finance(skip_finance=False, only_market=True, has_finance=True) is False
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_storage_fetch.py -q`

Expected: FAIL because `replace_on` and `should_collect_finance` do not exist.

- [ ] **Step 3: Implement idempotent insert and finance guard**

Add optional `replace_on` to `DuckDBStore.insert_rows`. Add `should_collect_finance()` to `fetch_all.py`, and skip finance with a warning when the collector is unavailable.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_storage_fetch.py -q`

Expected: PASS.

---

### Task 4: Normalized Views

**Files:**
- Create: `D:/accio/stock_data/trade_system/normalize.py`
- Create: `D:/accio/stock_data/scripts/build_normalized_views.py`
- Test: `D:/accio/stock_data/tests/test_normalize.py`

- [ ] **Step 1: Write failing normalized-view tests**

```python
import duckdb
from trade_system.normalize import build_normalized_views


def test_build_normalized_views_dedupes_market_daily(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, fall_count INTEGER, consecutive_count INTEGER, raw_json VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-06', 10, 2, 3000, 1000, 4, '{}', '2026-07-06 09:00:00'), ('2026-07-06', 11, 3, 3100, 900, 5, '{}', '2026-07-06 15:00:00')")
    con.close()
    build_normalized_views(str(db_path))
    con = duckdb.connect(str(db_path))
    rows = con.execute("SELECT trade_date, limit_up_count FROM v_market_daily").fetchall()
    con.close()
    assert rows == [("2026-07-06", 11)]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_normalize.py -q`

Expected: FAIL because normalize module does not exist.

- [ ] **Step 3: Implement normalized views**

Create deduped views for market daily, sector daily, limit pool, LHB daily, stock pool, and data coverage. Missing source tables create empty compatible views.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_normalize.py -q`

Expected: PASS.

---

### Task 5: Signal and Risk Tables

**Files:**
- Create: `D:/accio/stock_data/trade_system/signals.py`
- Create: `D:/accio/stock_data/trade_system/risk.py`
- Create: `D:/accio/stock_data/scripts/generate_signals.py`
- Create: `D:/accio/stock_data/scripts/init_trading_tables.py`
- Test: `D:/accio/stock_data/tests/test_signals_risk.py`

- [ ] **Step 1: Write failing signal/risk tests**

```python
import duckdb
from trade_system.normalize import build_normalized_views
from trade_system.signals import generate_signals
from trade_system.risk import init_trading_tables


def make_db(path):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE daily_summary(date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, fall_count INTEGER, consecutive_count INTEGER, raw_json VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-06', 85, 3, 3600, 900, 8, '{}', '2026-07-06 15:00:00')")
    con.execute("CREATE TABLE sector_strength(date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, fengban_rate DOUBLE, dieting INTEGER, up_count INTEGER, down_count INTEGER, raw_json VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO sector_strength VALUES ('2026-07-06', '801001', 88, 12, 76, 0, 40, 5, '{}', '2026-07-06 15:00:00')")
    con.execute("CREATE TABLE sector_ranking(date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER, fetched_at TIMESTAMP, raw_json VARCHAR)")
    con.execute("INSERT INTO sector_ranking VALUES ('2026-07-06', '801001', '测试板块', 20, '2026-07-06 15:00:00', '{}')")
    con.close()


def test_generate_signals_creates_market_regime_and_sector_scores(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    make_db(db_path)
    build_normalized_views(str(db_path))
    generate_signals(str(db_path), "2026-07-06")
    con = duckdb.connect(str(db_path))
    regime = con.execute("SELECT regime, suggested_position_pct FROM market_regime_snapshot").fetchone()
    sector = con.execute("SELECT sector_code, score FROM sector_rotation_score").fetchone()
    con.close()
    assert regime == ("高潮", 30)
    assert sector[0] == "801001"
    assert sector[1] > 0


def test_init_trading_tables_creates_operator_tables(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    init_trading_tables(str(db_path))
    con = duckdb.connect(str(db_path))
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    con.close()
    assert {"watchlist", "trade_plan", "portfolio_snapshot", "trade_journal", "risk_snapshot"} <= tables
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_signals_risk.py -q`

Expected: FAIL because signal and risk modules do not exist.

- [ ] **Step 3: Implement signal and risk modules**

Create market regime scoring from limit-up/down, rise/fall, broken rate, and consecutive count. Create sector scores from strength, limit-up count,封板率, and ranking. Create alert events for high drawdown risk, weak market, missing data, and overheat. Create operator tables.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_signals_risk.py -q`

Expected: PASS.

---

### Task 6: Backtest and Review Reports

**Files:**
- Create: `D:/accio/stock_data/trade_system/backtest.py`
- Create: `D:/accio/stock_data/trade_system/review.py`
- Create: `D:/accio/stock_data/scripts/run_backtest.py`
- Create: `D:/accio/stock_data/scripts/generate_review_report.py`
- Test: `D:/accio/stock_data/tests/test_backtest_review.py`

- [ ] **Step 1: Write failing report tests**

```python
import duckdb
from trade_system.backtest import run_market_regime_backtest
from trade_system.review import render_daily_report


def test_backtest_returns_sample_count(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, fall_count INTEGER, consecutive_count INTEGER, raw_json VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-05', 10, 40, 1000, 3500, 2, '{}', '2026-07-05 15:00:00'), ('2026-07-06', 80, 5, 3600, 900, 7, '{}', '2026-07-06 15:00:00')")
    con.close()
    result = run_market_regime_backtest(str(db_path))
    assert result["sample_count"] == 2
    assert "regime_counts" in result


def test_render_daily_report_contains_core_sections():
    report = render_daily_report(
        trade_date="2026-07-06",
        quality={"summary": {"table_count": 2, "total_rows": 10}},
        regime={"regime": "主升", "suggested_position_pct": 70},
        sectors=[{"sector_name": "测试板块", "score": 88}],
        candidates=[{"stock_name": "测试股票", "score": 77}],
        alerts=[{"severity": "P1", "message": "测试告警"}],
        backtest={"sample_count": 2, "regime_counts": {"主升": 1}},
    )
    assert "盘前/盘后交易辅助报告" in report
    assert "市场状态" in report
    assert "风险告警" in report
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_backtest_review.py -q`

Expected: FAIL because backtest and review modules do not exist.

- [ ] **Step 3: Implement backtest and review modules**

Backtest counts historical market-regime classifications available from existing daily data. Review report renders Markdown with data quality, market regime, sector scores, candidates, alerts, and backtest summary.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_backtest_review.py -q`

Expected: PASS.

---

### Task 7: End-to-End Verification

**Files:**
- No new production files unless earlier tasks expose defects.

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Build normalized views on current DB**

Run: `python scripts/build_normalized_views.py --db kpl_data.duckdb`

Expected: prints created view names and exits 0.

- [ ] **Step 3: Generate signals for latest date**

Run: `python scripts/generate_signals.py --db kpl_data.duckdb`

Expected: prints latest trade date, regime, sector count, candidate count, and alert count.

- [ ] **Step 4: Initialize trading tables**

Run: `python scripts/init_trading_tables.py --db kpl_data.duckdb`

Expected: creates watchlist, trade plan, portfolio, journal, and risk snapshot tables.

- [ ] **Step 5: Generate reports**

Run: `python scripts/audit_data_quality.py --db kpl_data.duckdb --schema schema.py --out reports/data_quality_latest.md`

Run: `python scripts/run_backtest.py --db kpl_data.duckdb --out reports/backtest_latest.md`

Run: `python scripts/generate_review_report.py --db kpl_data.duckdb --out reports/daily_trading_report_latest.md`

Expected: all three Markdown reports are created and non-empty.

---

## Self-Review

- Spec coverage: phases 1-5 are covered by Tasks 1-7.
- Placeholder scan: no placeholder steps; each task has exact files, commands, and expected results.
- Type consistency: module names and function names are defined before use in later tasks.
- Known adaptation: this directory is not a git repository, so commit steps are replaced by verification checkpoints.
