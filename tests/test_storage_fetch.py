from base import DuckDBStore
from fetch_all import should_collect_finance
import pytest


def test_insert_rows_can_replace_existing_key_rows(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    store = DuckDBStore(str(db_path))
    store.insert_rows(
        "market_mood",
        [("2026-07-06", 1)],
        ["date", "rise_count"],
        replace_on=["date"],
    )
    store.insert_rows(
        "market_mood",
        [("2026-07-06", 2)],
        ["date", "rise_count"],
        replace_on=["date"],
    )
    rows = store.fetchall("SELECT date, rise_count FROM market_mood")
    store.close()
    assert rows == [("2026-07-06", "2")]


def test_insert_rows_failure_does_not_report_or_persist_partial_batch(tmp_path):
    db_path = tmp_path / "atomic.duckdb"
    store = DuckDBStore(str(db_path))
    store.execute("CREATE TABLE guarded(value INTEGER CHECK(value > 0))")
    with pytest.raises(Exception):
        store.insert_rows("guarded", [(1,), (-1,)], ["value"])
    assert store.fetchall("SELECT * FROM guarded") == []
    store.close()


def test_insert_rows_respects_caller_transaction(tmp_path):
    db_path = tmp_path / "nested.duckdb"
    store = DuckDBStore(str(db_path))
    store.execute("CREATE TABLE nested(value INTEGER)")
    store.execute("BEGIN TRANSACTION")
    store.insert_rows("nested", [(1,), (2,)], ["value"])
    store.execute("COMMIT")
    assert store.fetchall("SELECT value FROM nested ORDER BY value") == [(1,), (2,)]
    store.close()


def test_should_collect_finance_requires_collector_and_flag():
    assert should_collect_finance(skip_finance=False, only_market=False, has_finance=True) is True
    assert should_collect_finance(skip_finance=False, only_market=False, has_finance=False) is False
    assert should_collect_finance(skip_finance=True, only_market=False, has_finance=True) is False
    assert should_collect_finance(skip_finance=False, only_market=True, has_finance=True) is False
