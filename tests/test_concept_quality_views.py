import duckdb

from trade_system.schema import init_schema


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


def test_default_concept_views_reject_unchecked_kpl_snapshot(tmp_path, monkeypatch, capsys):
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

    # The current collector must not certify a failed replacement, and its
    # metering must follow real commits rather than attempted member inserts.
    import json
    import sys
    from types import SimpleNamespace
    import pytest
    from scripts import collect_ths_concepts_api as collector
    from trade_system.collection_profiles import read_product_counts
    context = {'demand_id': 'concept-transaction-test'}
    monkeypatch.setenv('STOCKDATA_REQUEST_CONTEXT', json.dumps(context))
    monkeypatch.setattr(sys, 'argv', ['collector', '--db', str(tmp_path / 'current.duckdb')])
    catalog = [{'thscode': '885001.TI', 'name': 'A'}, {'thscode': '885002.TI', 'name': 'B'}]
    broken = {'ticker': '000002', 'name': {'unserializable'}}
    calls = []
    def constituents(code):
        calls.append(code)
        return ([{'ticker': '000001', 'name': 'ok'}] * 2 if code == '885001.TI' else [broken])
    client = SimpleNamespace(ths_concept_catalog=lambda **_: catalog,
                             ths_index_constituents=constituents, quota_note='fixture')
    monkeypatch.setattr(collector, 'HiThinkClient', lambda **_: client)
    with pytest.raises(TypeError):
        collector.main()
    counts = read_product_counts(capsys.readouterr().out, context)['scopes']
    assert counts['ths_concept_daily']['rows_written'] == 1
    assert counts['ths_concept_stock_history']['rows_written'] == 1
    assert counts['ths_concept_stock_history']['rows_parsed'] == 3
    with duckdb.connect(str(tmp_path / 'current.duckdb')) as check:
        for table in ('ths_concept_daily', 'ths_concept_stock_history', 'ths_concept_member_checkpoint'):
            assert check.execute(f'SELECT concept_code FROM {table}').fetchall() == [('THS-885001',)]
    broken['name'] = 'repaired'
    # A two-concept fixture still fails the full-market quality floor.
    assert collector.main() == 2
    counts = read_product_counts(capsys.readouterr().out, context)['scopes']
    assert counts['ths_concept_daily']['rows_written'] == 1
    assert counts['ths_concept_stock_history']['rows_written'] == 1
    assert counts['ths_concept_stock_history']['receipt_reused'] is True
    assert collector.main() == 2
    counts = read_product_counts(capsys.readouterr().out, context)['scopes']
    assert counts['ths_concept_stock_history']['rows_written'] == 0
    assert counts['ths_concept_stock_history']['rows_parsed'] == 0
    assert calls == ['885001.TI', '885002.TI', '885002.TI']

    con = duckdb.connect(str(db_path), read_only=True)
    assert con.execute(
        "SELECT concept_code FROM v_default_concept_daily WHERE trade_date='2026-08-06'"
    ).fetchall() == []
    assert con.execute(
        "SELECT concept_code, stock_code FROM v_default_concept_stock_history WHERE trade_date='2026-08-06'"
    ).fetchall() == []
    con.close()
