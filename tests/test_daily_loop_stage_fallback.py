"""Missing scores/stage/account evidence must never revive the old writer."""
import pytest
from trade_system.daily_loop import run_daily_operator_loop


@pytest.mark.parametrize("stage", ["auction", "intraday", "close"])
def test_no_stage_fallback_can_initialize_an_account_or_plan(tmp_path, stage):
    db=tmp_path/"absent.duckdb"
    with pytest.raises(RuntimeError, match="retired"):
        run_daily_operator_loop(db,"2026-09-10",20,stage=stage)
    assert not db.exists()
