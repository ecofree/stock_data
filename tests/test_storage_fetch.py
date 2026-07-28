from base import DuckDBStore
from fetch_all import should_collect_finance


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


def test_should_collect_finance_requires_collector_and_flag():
    assert should_collect_finance(skip_finance=False, only_market=False, has_finance=True) is True
    assert should_collect_finance(skip_finance=False, only_market=False, has_finance=False) is False
    assert should_collect_finance(skip_finance=True, only_market=False, has_finance=True) is False
    assert should_collect_finance(skip_finance=False, only_market=True, has_finance=True) is False
