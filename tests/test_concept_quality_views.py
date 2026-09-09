import duckdb

from schema import init_schema


def test_default_concept_views_exclude_stale_and_unchecked_snapshots(tmp_path):
    db_path = tmp_path / "quality_views.duckdb"
    con = duckdb.connect(str(db_path))
    init_schema(con)
    con.execute(
        "INSERT INTO ths_concept_snapshot_expectation "
        "(trade_date, expected_concepts, provider, catalog_hash) "
        "VALUES ('2026-08-07', 374, 'test_catalog', 'test')"
    )
    # The production contract requires a complete 374-board snapshot.  Keep
    # three negative rows in the same date to prove they are excluded while
    # the 374 verified rows remain eligible.
    good = [
        ("2026-08-07", f"THS-{i:03d}", f"概念{i}", i, 1, "ths",
         '{"stale_fallback":false,"fetched_date":"2026-08-07"}', True)
        for i in range(1, 375)
    ]
    con.executemany(
        "INSERT INTO ths_concept_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
        good,
    )
    con.executemany(
        "INSERT INTO ths_concept_stock_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
            [
                ("2026-08-07", f"THS-{i:03d}", f"概念{i}", f"{i:06d}", f"股票{i}", 1,
                 "ths", '{"stale_fallback":false,"fetched_date":"2026-08-07"}', True)
                for i in range(1, 375)
            ] + [
                ("2026-08-07", "THS-001", "概念1", "100001", "补充股票1", 2,
                 "ths", '{"stale_fallback":false,"fetched_date":"2026-08-07"}', True),
                ("2026-08-07", "THS-002", "概念2", "100002", "补充股票2", 2,
                 "ths", '{"stale_fallback":false,"fetched_date":"2026-08-07"}', True),
            ] + [
            ("2026-08-08", "THS-STALE", "过期", "999991", "过期A", 1,
             "ths_cached_weekly", '{"stale_fallback":true,"fetched_date":"2026-08-08"}', False),
            ("2026-08-08", "THS-PARTIAL", "部分", "999992", "部分A", 1,
             "ths", '{"stale_fallback":false,"fetched_date":"2026-08-08"}', False),
            ("2026-08-08", "THS-UNVERIFIED", "未验证", "999993", "未验证A", 1,
             "ths", '{"stale_fallback":false,"fetched_date":"2026-08-08"}', False),
        ],
    )
    con.executemany(
        "INSERT INTO ths_concept_member_checkpoint "
        "(trade_date, concept_code, concept_name, status, member_rows) VALUES (?, ?, ?, ?, 1)",
        [("2026-08-07", f"THS-{i:03d}", f"概念{i}", "success") for i in range(1, 375)] + [
            ("2026-08-08", "THS-STALE", "过期", "success_stale"),
            ("2026-08-08", "THS-PARTIAL", "部分", "partial"),
            ("2026-08-08", "THS-UNVERIFIED", "未验证", "success"),
        ],
    )
    rows = con.execute(
        "SELECT concept_code FROM v_default_concept_daily WHERE trade_date='2026-08-07' ORDER BY concept_code"
    ).fetchall()
    members = con.execute(
        "SELECT concept_code, stock_code FROM v_default_concept_stock_history "
        "WHERE trade_date='2026-08-07' ORDER BY concept_code"
    ).fetchall()
    con.close()

    assert len(rows) == 374
    assert rows[0] == ("THS-001",)
    assert ("THS-STALE",) not in rows
    assert ("THS-PARTIAL",) not in rows
    assert ("THS-UNVERIFIED",) not in rows
    assert len(members) == 376
    assert members[0] == ("THS-001", "000001")


def test_default_concept_views_reject_unchecked_kpl_snapshot(tmp_path):
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
    ).fetchall() == []
    assert con.execute(
        "SELECT concept_code, stock_code FROM v_default_concept_stock_history WHERE trade_date='2026-08-06'"
    ).fetchall() == []
    con.close()
