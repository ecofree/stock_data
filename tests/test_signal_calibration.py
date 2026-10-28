import json

import duckdb

from trade_system.normalize import build_normalized_views


def _calibration_db(path, *, weak_market: bool = False, with_provenance: bool = True):
    con = duckdb.connect(str(path))
    if weak_market:
        daily = ("2026-07-06", 12, 65, 900, 3600, 1)
        rise_fall = ("2026-07-06", 12, 65, 45, 80, 76.0)
    else:
        daily = ("2026-07-06", 55, 8, 3000, 1200, 5)
        rise_fall = ("2026-07-06", 55, 8, 10, 12, 20.0)
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, "
        "fall_count INTEGER, consecutive_count INTEGER, fetched_at TIMESTAMP, "
        "source_kind VARCHAR, count_provenance_json VARCHAR)"
    )
    proof = json.dumps({field: {'value': value, 'source': 'daily_summary', 'source_kind': 'real',
        'source_date': daily[0], 'fetched_at': '2026-07-06T15:00:00', 'qualified': True,
        'freshness_basis': 'source_relation_receipt_lower_bound'} for field, value in zip(
            ('limit_up_count', 'limit_down_count', 'rise_count', 'fall_count', 'consecutive_count'),
            daily[1:])})
    con.execute(
        "INSERT INTO daily_summary VALUES (?, ?, ?, ?, ?, ?, '2026-07-06 15:00:00', 'real', ?)",
        (*daily, proof if with_provenance else None),
    )
    con.execute(
        "CREATE TABLE market_rise_fall("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, broken_limit_up_count INTEGER, "
        "blown_limit_up_count INTEGER, blown_limit_up_rate DOUBLE, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO market_rise_fall VALUES (?, ?, ?, ?, ?, ?, '2026-07-06 15:00:00')",
        rise_fall,
    )
    con.close()


def test_acute_drop_risk_is_normalized_to_100_point_scale(tmp_path):
    db_path = tmp_path / "risk_scale.duckdb"
    _calibration_db(db_path, weak_market=True)
    build_normalized_views(db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        score = con.execute("SELECT acute_drop_risk_score FROM v_market_state_inputs").fetchone()[0]
    finally:
        con.close()

    assert 0 <= score <= 100


def test_acute_drop_risk_without_count_provenance_remains_unqualified(tmp_path):
    db_path = tmp_path / "unverified_risk_scale.duckdb"
    _calibration_db(db_path, weak_market=True, with_provenance=False)
    build_normalized_views(db_path)
    with duckdb.connect(str(db_path), read_only=True) as con:
        row = con.execute(
            "SELECT limit_up_count, limit_down_count, rise_count, fall_count, consecutive_count, "
            "acute_drop_risk_score, acute_drop_risk_status FROM v_market_state_inputs"
        ).fetchone()
    assert row == (12, 65, 900, 3600, 1, None, 'unverified')
