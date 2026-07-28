from scripts.collect_capital_flow_focus import _current_flow_rows


def test_current_flow_rows_rejects_stale_or_undated_stock_snapshots():
    row = {"date": "20260713", "main_net": 10}
    assert _current_flow_rows([row], "2026-07-14") == []
    assert _current_flow_rows([{**row, "date": "20260714"}], "2026-07-14") == [
        {"date": "20260714", "main_net": 10}
    ]
    assert _current_flow_rows([{"main_net": 10}], "2026-07-14") == []


def test_current_flow_rows_allows_explicitly_undated_sector_snapshot():
    assert _current_flow_rows(
        [{"sector_code": "BK0001", "main_net": 10}],
        "2026-07-14",
        allow_undated_snapshot=True,
    )
