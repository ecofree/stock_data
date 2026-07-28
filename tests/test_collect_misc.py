from collect_misc import _normalize_kline_date, _parse_bidding_anomalies


def test_normalize_kline_date_accepts_compact_api_date():
    assert _normalize_kline_date("20260701") == "2026-07-01"
    assert _normalize_kline_date("2026-07-01") == "2026-07-01"


def test_parse_bidding_anomalies_accepts_api_array_shape():
    rows = _parse_bidding_anomalies(
        {
            "date": "2026-07-07",
            "anomalies": [["002384", "东山精密", 0, 0, 132805637, 0, 1783353600]],
        },
        "2026-07-06",
    )

    assert rows == [("2026-07-07", "002384", "bidding_amount", 132805637.0)]
