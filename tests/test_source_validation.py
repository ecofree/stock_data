from trade_system.source_validation import validate_kpl
import time

from trade_system.resilient_sources import _call


def test_daily_all_zero_placeholder_is_rejected():
    result = validate_kpl(
        "/daily",
        {"date": "2026-07-15"},
        {"date": "2026-07-15", "上涨家数": 0, "下跌家数": 0, "涨停家数": 0},
    )
    assert not result.ok
    assert "zero" in result.reason


def test_rise_fall_previous_day_is_rejected():
    result = validate_kpl(
        "/market/rise-fall",
        {"date": "2026-07-15"},
        {"raw_data": [[79, 22, 3, 76, 3.8, 20, "2026-07-14"]]},
    )
    assert not result.ok
    assert "2026-07-15" in result.reason


def test_realtime_board_payload_requires_rows():
    assert not validate_kpl("/l2/realtime/all-boards", {}, {"data": []}).ok
    assert validate_kpl("/l2/realtime/all-boards", {}, {"data": [{"code": "000001"}]}).ok


def test_realtime_board_grouped_payload_is_valid():
    payload = {
        "first_board": [{"stock_code": "000001"}],
        "second_board": [],
        "statistics": {"total": 1},
    }
    assert validate_kpl("/l2/realtime/all-boards", {}, payload).ok


def test_realtime_index_list_payload_is_valid():
    payload = {"indexes": [{"index_code": "000001"}], "raw_data": []}
    assert validate_kpl("/l2/realtime/index-list", {}, payload).ok


def test_provider_timeout_does_not_wait_for_blocked_worker():
    started = time.monotonic()
    result = _call(lambda: (time.sleep(0.3), {"ok": True})[1], timeout=0.03, retries=0)
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 0.2
