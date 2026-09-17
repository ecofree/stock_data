import pytest
from scripts import collect_tushare_basic_data, run_daily_screen, generate_web_dashboard

@pytest.mark.parametrize("module", [collect_tushare_basic_data,run_daily_screen,generate_web_dashboard])
def test_retired_entry_points_refuse_before_work(module):
    with pytest.raises(SystemExit,match="retired entry point"):
        module.main()
