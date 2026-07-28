from collect_l2 import _extract_index_rows, _intraday_main_net


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
