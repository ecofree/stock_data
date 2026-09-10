"""Retired generators cannot edit manual/account facts, including on retry."""
import duckdb
import pytest
from trade_system.daily_loop import run_daily_operator_loop


@pytest.mark.parametrize("stage", ["auction", "intraday", "close"])
@pytest.mark.parametrize("limit", [0, 20])
def test_legacy_refresh_preserves_all_operator_facts(tmp_path, stage, limit):
    db=tmp_path/"manual.duckdb"
    with duckdb.connect(str(db)) as con:
        for table in ("watchlist", "trade_plan", "portfolio_snapshot", "trade_journal"):
            con.execute(f"CREATE TABLE {table}(note VARCHAR)")
            con.execute(f"INSERT INTO {table} VALUES (?)", ["manual condition unchanged"])
    before=db.read_bytes()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="retired"):
            run_daily_operator_loop(db,"2026-09-10",limit,stage)
    assert before==db.read_bytes()
