from datetime import datetime, timezone

from collectors.collect_l2 import _extract_index_rows, _intraday_main_net, _tick_source_date


def test_extract_index_rows_accepts_indexes_payload():
    rows = _extract_index_rows(
        {
            "indexes": [
                {
                    "stock_id": "SH000001",
                    "name": "上证指数",
                    "value": 3990.24,
                    "change_pct": -1.26,
                }
            ]
        },
        "2026-07-07",
    )

    assert rows == [("2026-07-07", "SH000001", "上证指数", 3990.24, -1.26)]


def test_intraday_main_net_accepts_api_field_name():
    assert _intraday_main_net({"main_net_inflow": 123}) == 123
    assert _intraday_main_net({"main_fund_net": 456}) == 456


def test_tick_date_uses_returned_epoch_and_shanghai_session_not_observation_day():
    # 16:01 UTC is already the next session date in Asia/Shanghai.
    stamp = int(datetime(2026, 7, 5, 16, 1, tzinfo=timezone.utc).timestamp())
    assert _tick_source_date({"timestamp": str(stamp)}) == "20260706"
    assert _tick_source_date({"timestamp": str(stamp * 1000)}) == "20260706"
    assert _tick_source_date({"timestamp": str(stamp)}, allow_epoch=False) is None
    assert _tick_source_date({"timestamp": "09:31:00"}) is None
    assert _tick_source_date({"date": "2026076"}) is None
    assert _tick_source_date({"date": "20260230", "timestamp": str(stamp)}) is None
