import duckdb

from schema import init_schema


def test_default_concept_views_exclude_stale_and_unchecked_snapshots(tmp_path):
    db_path = tmp_path / "quality_views.duckdb"
    con = duckdb.connect(str(db_path))
    init_schema(con)
    con.execute(
        "INSERT INTO ths_concept_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp), "
        "(?, ?, ?, ?, ?, ?, ?, ?, current_timestamp), "
        "(?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
        [
            "2026-08-07", "THS-OK", "可用", 1, 1, "ths", '{"stale_fallback": false}', True,
            "2026-08-07", "THS-STALE", "过期", 2, 1, "ths_cached_weekly", '{"stale_fallback": true}', False,
            "2026-08-07", "THS-PARTIAL", "部分", 3, 1, "ths", '{"stale_fallback": false}', False,
        ],
    )
    con.execute(
        "INSERT INTO ths_concept_stock_history VALUES "
        "('2026-08-07','THS-OK','可用','000001','平安银行',1,'ths','{\"stale_fallback\":false}',true,current_timestamp), "
        "('2026-08-07','THS-STALE','过期','000002','万科A',1,'ths_cached_weekly','{\"stale_fallback\":true}',false,current_timestamp), "
        "('2026-08-07','THS-PARTIAL','部分','000003','国农科技',1,'ths','{\"stale_fallback\":false}',false,current_timestamp)",
    )
    con.execute(
        "INSERT INTO ths_concept_member_checkpoint "
        "(trade_date, concept_code, concept_name, status, member_rows) VALUES "
        "('2026-08-07','THS-OK','可用','success',1), "
        "('2026-08-07','THS-STALE','过期','success_stale',1), "
        "('2026-08-07','THS-PARTIAL','部分','partial',1)"
    )
    rows = con.execute(
        "SELECT concept_code FROM v_default_concept_daily WHERE trade_date='2026-08-07' ORDER BY concept_code"
    ).fetchall()
    members = con.execute(
        "SELECT concept_code, stock_code FROM v_default_concept_stock_history "
        "WHERE trade_date='2026-08-07' ORDER BY concept_code"
    ).fetchall()
    con.close()

    assert rows == [("THS-OK",)]
    assert members == [("THS-OK", "000001")]


def test_default_concept_views_use_kpl_only_when_no_valid_ths_snapshot(tmp_path):
    db_path = tmp_path / "kpl_fallback.duckdb"
    con = duckdb.connect(str(db_path))
    init_schema(con)
    con.execute(
        "INSERT INTO kpl_concept_daily VALUES "
        "('2026-08-06','KPL-1','兼容概念',1,1,'kpl','{}',false,current_timestamp)"
    )
    con.execute(
        "INSERT INTO kpl_concept_stock_history VALUES "
        "('2026-08-06','KPL-1','兼容概念','000001','平安银行',1,'kpl','{}',false,current_timestamp)"
    )
    con.close()

    con = duckdb.connect(str(db_path), read_only=True)
    assert con.execute(
        "SELECT concept_code FROM v_default_concept_daily WHERE trade_date='2026-08-06'"
    ).fetchall() == [("KPL-1",)]
    assert con.execute(
        "SELECT concept_code, stock_code FROM v_default_concept_stock_history WHERE trade_date='2026-08-06'"
    ).fetchall() == [("KPL-1", "000001")]
    con.close()
