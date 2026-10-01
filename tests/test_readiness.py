import duckdb
import pytest
from datetime import datetime

from trade_system.readiness import assess_trade_date_readiness, capital_flow_coverage, relation_freshness


def _batch_evidence(con, kind, day, expected=1, observed=1, coverage=100, status="success"):
    con.execute(f"CREATE TABLE IF NOT EXISTS intraday_{kind}_flow_batch("
                "trade_date DATE,expected_rows INTEGER,fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR)")
    con.execute(f"INSERT INTO intraday_{kind}_flow_batch VALUES (?,?,?,?,?)",
                [day, expected, observed, coverage, status])


def _qualified_stock(con, day, codes=("000001",)):
    con.execute("CREATE TABLE multi_source_stock_flow(source_date DATE,stock_code VARCHAR,"
                "main_net DOUBLE,fetched_at TIMESTAMP)")
    for code in codes:
        con.execute("INSERT INTO multi_source_stock_flow VALUES (?,?,100,?)", [day, code, day + " 10:00:00"])
    _batch_evidence(con, "stock", day, len(codes), len(codes))


def _qualified_sector(con, day):
    con.execute("CREATE TABLE multi_source_sector_flow(source_date DATE,sector_code VARCHAR,"
                "sector_type VARCHAR,main_net DOUBLE,amount_unit VARCHAR,fetched_at TIMESTAMP)")
    con.execute("INSERT INTO multi_source_sector_flow VALUES (?, '801001', 'em_industry', 100, 'yuan', ?),"
                "(?, 'THS-1', 'ths_concept_derived', 100, 'yuan', ?)",
                [day, day + " 10:00:00", day, day + " 10:00:00"])
    con.execute("CREATE VIEW v_sector_capital AS SELECT source_date trade_date,sector_code,"
                "main_net main_net_inflow,fetched_at FROM multi_source_sector_flow")
    _batch_evidence(con, "sector", day, 2, 2)
    con.execute("CREATE TABLE intraday_sector_flow_taxonomy(trade_date DATE,taxonomy VARCHAR,"
                "expected_rows INTEGER,fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR)")
    con.execute("INSERT INTO intraday_sector_flow_taxonomy VALUES (?, 'em_industry',1,1,100,'success'),"
                "(?, 'ths_concept',1,1,100,'success')", [day, day])


def test_unknown_required_sector_denominator_never_borrows_other_product_coverage():
    from trade_system.readiness import required_sector_taxonomy_coverage
    con=duckdb.connect(':memory:')
    assert not required_sector_taxonomy_coverage(con,'2026-09-29')['passed']
    con.execute('CREATE TABLE intraday_sector_flow_taxonomy(trade_date DATE,taxonomy VARCHAR,expected_rows INTEGER,fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR)')
    con.execute("INSERT INTO intraday_sector_flow_taxonomy VALUES ('2026-09-29','em_industry',496,496,100,'success'),('2026-09-29','ths_concept',0,0,0,'missing')")
    result=required_sector_taxonomy_coverage(con,'2026-09-29')
    assert result['taxonomies']['em_industry']['passed']
    assert not result['passed'] and result['taxonomies']['ths_concept']['coverage_pct'] is None
    con.execute("UPDATE intraday_sector_flow_taxonomy SET expected_rows=390,fetched_rows=390,coverage_pct=100,status='success' WHERE taxonomy='ths_concept'")
    assert required_sector_taxonomy_coverage(con,'2026-09-29')['passed']
    con.close()


def test_historical_as_of_rejects_rows_written_after_the_audit_time(tmp_path):
    db_path = tmp_path / "future-row.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow("
        "source_date DATE,stock_code VARCHAR,main_net DOUBLE,fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "('2026-07-31','000001',100,'2026-08-01 09:00:00')"
    )
    con.close()

    result = assess_trade_date_readiness(
        db_path,
        "2026-07-31",
        "intraday",
        required_groups=["stock_capital_flow"],
        max_age_seconds=7200,
        now=datetime.fromisoformat("2026-07-31T17:45:00"),
    )

    assert result["ready"] is False
    assert result["missing_groups"] == ["stock_capital_flow"]


def test_readiness_aware_as_of_is_comparable_to_naive_db_time(tmp_path):
    db_path = tmp_path / "aware.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE daily_summary(date DATE, limit_up_count INTEGER, fetched_at TIMESTAMP, source_kind VARCHAR)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES ('2026-07-31', 10, '2026-07-31 17:45:00', 'real')"
    )
    con.close()
    result = assess_trade_date_readiness(
        db_path,
        "2026-07-31",
        required_groups=("market_state",),
        max_age_seconds=1200,
        now=datetime.fromisoformat("2026-07-31T18:00:00+08:00"),
    )
    assert result["ready"] is True
    assert result["as_of"] == "2026-07-31 18:00:00"


def test_relation_freshness_casts_legacy_varchar_timestamp(tmp_path):
    db_path = tmp_path / "varchar-time.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE multi_source_stock_flow("
        "source_date DATE,stock_code VARCHAR,main_net DOUBLE,fetched_at VARCHAR)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "('2026-07-31','000001',100,'2026-07-31 17:45:00')"
    )
    _batch_evidence(con, "stock", "2026-07-31")
    result = relation_freshness(
        con,
        "multi_source_stock_flow",
        "2026-07-31",
        max_age_seconds=3600,
        now=datetime.fromisoformat("2026-07-31T18:00:00"),
    )
    con.close()

    assert result["rows"] == 1
    assert result["status"] == "ready"


def test_close_readiness_requires_same_date_capital_flows(tmp_path):
    db_path = tmp_path / "readiness.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO kline VALUES ('2026-07-09','000001')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-08','801001')")
    con.execute("CREATE TABLE l2_stock_intraday(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO l2_stock_intraday VALUES ('2026-07-09','000001')")
    _qualified_stock(con, "2026-07-09")
    _qualified_sector(con, "2026-07-08")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "close")

    assert result["ready"] is False
    assert result["missing_groups"] == ["sector_capital_flow"]
    with duckdb.connect(str(db_path)) as con:
        con.execute('CREATE TABLE tushare_stock_basic(ts_code VARCHAR,list_date DATE,delist_date DATE)')
        con.execute("INSERT INTO tushare_stock_basic SELECT 'old'||i,'2000-01-01','2026-07-01' FROM range(2000) t(i)")
        con.execute("INSERT INTO tushare_stock_basic VALUES ('000001.SZ','2000-01-01',NULL),('new','2026-07-10',NULL)")
    result = assess_trade_date_readiness(db_path, '2026-07-09', 'close')
    assert result['missing_groups'] == ['sector_capital_flow']


def test_intraday_readiness_passes_when_both_capital_flows_are_current(tmp_path):
    db_path = tmp_path / "intraday.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-09','801001')")
    con.execute("CREATE TABLE l2_stock_bigorder(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO l2_stock_bigorder VALUES ('2026-07-09','000001')")
    _qualified_stock(con, "2026-07-09", ("000001", "000002"))
    _qualified_sector(con, "2026-07-09")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "intraday")

    assert result["ready"] is True
    assert result["missing_groups"] == []


def test_intraday_readiness_uses_same_date_market_stock_flow(tmp_path):
    db_path = tmp_path / "market-flow.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-09','801001')")
    _qualified_stock(con, "2026-07-09")
    _qualified_sector(con, "2026-07-09")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "intraday")

    assert result["ready"] is True
    stock_group = next(item for item in result["groups"] if item["group"] == "stock_capital_flow")
    assert stock_group["selected_relation"] == "multi_source_stock_flow"


def test_partial_market_batch_cannot_be_replaced_by_bounded_flow(tmp_path):
    db_path = tmp_path / "partial-market-flow.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-09')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-09' trade_date")
    con.execute("CREATE TABLE realtime_candidate_pool_snapshot(trade_date DATE, status VARCHAR, stock_count INTEGER)")
    con.execute("INSERT INTO realtime_candidate_pool_snapshot VALUES ('2026-07-09','success',2)")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-09','801001')")
    _qualified_sector(con, "2026-07-09")
    con.execute("CREATE TABLE multi_source_stock_flow(source_date DATE, stock_code VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO multi_source_stock_flow VALUES ('2026-07-09','000001', current_timestamp)")
    con.execute("CREATE TABLE intraday_stock_flow_batch(trade_date DATE, expected_rows INTEGER, fetched_rows INTEGER, coverage_pct DOUBLE, status VARCHAR)")
    con.execute("INSERT INTO intraday_stock_flow_batch VALUES ('2026-07-09',5000,1,0.02,'partial')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-09", "intraday")

    assert result["ready"] is False
    assert result["missing_groups"] == ["stock_capital_flow"]

    for coverage in (99.5, 100.0):
        con = duckdb.connect(str(db_path))
        con.execute(
            "UPDATE intraday_stock_flow_batch SET fetched_rows=5000, coverage_pct=?",
            [coverage],
        )
        con.close()
        result = assess_trade_date_readiness(db_path, "2026-07-09", "intraday")
        stock_group = next(item for item in result["groups"] if item["group"] == "stock_capital_flow")
        assert stock_group["ready"] is False
        assert stock_group["status"] == "partial"


@pytest.mark.parametrize("group,relation", [
    ("stock_capital_flow", name) for name in (
        "v_intraday_capital_flow_evidence", "multi_source_stock_flow", "l2_stock_intraday",
        "l2_stock_bigorder", "advanced_zjmm_min", "advanced_dadan_kline", "advanced_main_activity_kline",
    )
] + [("sector_capital_flow", "v_sector_capital"), ("sector_capital_flow", "sector_capital")])
def test_every_capital_relation_requires_dated_batch_evidence(group, relation):
    con = duckdb.connect(":memory:")
    con.execute(f'CREATE TABLE "{relation}"(trade_date DATE,main_net DOUBLE,fetched_at TIMESTAMP)')
    con.execute(f'INSERT INTO "{relation}" VALUES (\'2026-09-29\',100,\'2026-09-29 10:00:00\')')
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday", required_groups=[group],
                                         now=datetime(2026, 9, 29, 10, 1), max_age_seconds=300)
    assert not result["source_ready"]
    gate = result["groups"][0]
    assert not gate["ready"] and gate["coverage_reason"] == "batch_evidence_missing"
    assert gate["coverage"]["expected_rows"] is None
    fact = next(item for item in gate["relations"] if item["relation"] == relation)
    assert fact["rows"] == 1 and fact["freshness_age_seconds"] == 60
    assert fact["data_status"] == "ready" and fact["status"] == "partial"
    direct = relation_freshness(con, relation, "2026-09-29", now=datetime(2026, 9, 29, 10, 1))
    assert direct["status"] == "partial" and not direct["coverage"]["passed"]
    con.close()


@pytest.mark.parametrize("kind", ["stock", "sector"])
@pytest.mark.parametrize("state", ["missing_table", "empty_table", "other_day", "duplicate", "bad_schema"])
def test_capital_batch_absence_or_ambiguity_cannot_be_ready(kind, state):
    con = duckdb.connect(":memory:")
    (_qualified_stock if kind == "stock" else _qualified_sector)(con, "2026-09-29")
    table = f"intraday_{kind}_flow_batch"
    if state in {"missing_table", "bad_schema"}:
        con.execute(f"DROP TABLE {table}")
        if state == "bad_schema":
            con.execute(f"CREATE TABLE {table}(trade_date DATE,status VARCHAR)")
    elif state == "empty_table":
        con.execute(f"DELETE FROM {table}")
    elif state == "other_day":
        con.execute(f"UPDATE {table} SET trade_date='2026-09-28'")
    else:
        con.execute(f"INSERT INTO {table} SELECT * FROM {table}")
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday",
                                         required_groups=[f"{kind}_capital_flow"],
                                         now=datetime(2026, 9, 29, 10, 1))
    assert not result["source_ready"] and not result["groups"][0]["coverage"]["passed"]
    assert result["groups"][0]["coverage"]["expected_rows"] is None
    con.close()


@pytest.mark.parametrize("expected,observed,coverage", [
    (None, 1, 100), (0, 1, 100), (False, 1, 100), (2.5, 2, 100),
    (float("nan"), 1, 100), (float("inf"), 1, 100),
    (10, None, 100), (10, 9, 100), (10, 10, 90), (10, 11, 100), (10, 9.5, 100),
    (1000, 995, 100),
    (10, 10, None), (10, 10, 101), (10, 10, float("nan")), (10, 10, float("inf")),
])
def test_success_cannot_certify_invalid_or_incomplete_counts(expected, observed, coverage):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE intraday_stock_flow_batch(trade_date DATE,expected_rows DOUBLE,"
                "fetched_rows DOUBLE,coverage_pct DOUBLE,status VARCHAR)")
    con.execute("INSERT INTO intraday_stock_flow_batch VALUES ('2026-09-29',?,?,?,'success')",
                [expected, observed, coverage])
    assert not capital_flow_coverage(con, "2026-09-29", "stock")["passed"]
    con.close()


@pytest.mark.parametrize("state", ["missing_table", "missing_ths", "unknown_ths", "duplicate",
                                    "too_many_rows", "too_large_pct", "nan_pct", "partial"])
def test_required_sector_classification_cannot_borrow_another_products_success(state):
    con = duckdb.connect(":memory:")
    _qualified_sector(con, "2026-09-29")
    table = "intraday_sector_flow_taxonomy"
    if state == "missing_table":
        con.execute(f"DROP TABLE {table}")
    elif state == "missing_ths":
        con.execute(f"DELETE FROM {table} WHERE taxonomy='ths_concept'")
    elif state == "duplicate":
        con.execute(f"INSERT INTO {table} SELECT * FROM {table} WHERE taxonomy='ths_concept'")
    else:
        assignment = {"unknown_ths": "expected_rows=NULL", "too_many_rows": "fetched_rows=2",
                      "too_large_pct": "coverage_pct=101", "nan_pct": "coverage_pct='NaN'::DOUBLE",
                      "partial": "status='partial'"}[state]
        con.execute(f"UPDATE {table} SET {assignment} WHERE taxonomy='ths_concept'")
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday",
                                         required_groups=["sector_capital_flow"],
                                         now=datetime(2026, 9, 29, 10, 1))
    assert not result["source_ready"]
    current = next(item for item in result["groups"][0]["relations"] if item["relation"] == "v_sector_capital")
    assert current["rows"] == 2 and current["data_status"] == "ready"
    assert current["status"] == "partial"
    con.close()


@pytest.mark.parametrize("kind", ["stock", "sector"])
def test_current_bounded_row_cannot_cover_stale_canonical_input(kind):
    con = duckdb.connect(":memory:")
    (_qualified_stock if kind == "stock" else _qualified_sector)(con, "2026-09-29")
    if kind == "stock":
        con.execute("CREATE VIEW v_intraday_capital_flow_evidence AS SELECT DATE '2026-09-29' trade_date,"
                    "'000001' stock_code,100 main_net,TIMESTAMP '2026-09-29 13:00:00' fetched_at")
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday",
                                         required_groups=[f"{kind}_capital_flow"],
                                         max_age_seconds=300, now=datetime(2026, 9, 29, 13))
    assert not result["source_ready"]
    raw = next(item for item in result["groups"][0]["relations"]
               if item["relation"] == ("multi_source_stock_flow" if kind == "stock" else "v_sector_capital"))
    assert raw["rows"] == 0 and raw["same_date_rows"] > 0
    assert raw["freshness_age_seconds"] == 10800
    assert raw["status"] == "stale_or_empty" and raw["data_status"] == "stale_or_empty"
    assert not raw["coverage"]["actual"]["passed"]
    con.close()


def test_checkpoint_success_must_match_actual_distinct_stock_scope():
    con = duckdb.connect(":memory:")
    _qualified_stock(con, "2026-09-29", ("000001", "000001"))
    con.execute("CREATE TABLE l2_stock_intraday(date DATE,stock_code VARCHAR,fetched_at TIMESTAMP)")
    con.execute("INSERT INTO l2_stock_intraday VALUES ('2026-09-29','000002','2026-09-29 10:00:00')")
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday",
                                         required_groups=["stock_capital_flow"],
                                         now=datetime(2026, 9, 29, 10, 1))
    assert not result["source_ready"]
    coverage = result["groups"][0]["coverage"]
    assert coverage["fetched_rows"] == 2 and coverage["actual"]["observed_codes"] == 1
    assert coverage["actual"]["coverage_pct"] == 50
    con.close()


def test_actual_sector_scope_is_per_classification_and_cannot_borrow_or_duplicate_rows():
    con = duckdb.connect(":memory:")
    _qualified_sector(con, "2026-09-29")
    con.execute("DELETE FROM multi_source_sector_flow WHERE sector_type='ths_concept_derived'")
    con.execute("INSERT INTO multi_source_sector_flow SELECT * FROM multi_source_sector_flow")
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday",
                                         required_groups=["sector_capital_flow"],
                                         now=datetime(2026, 9, 29, 10, 1))
    assert not result["source_ready"]
    actual = result["groups"][0]["coverage"]["actual"]["taxonomies"]
    assert actual["em_industry"]["observed_codes"] == 1 and actual["em_industry"]["passed"]
    assert actual["ths_concept"]["observed_codes"] == 0 and not actual["ths_concept"]["passed"]
    con.close()


def test_canonical_stock_scope_must_match_the_checkpoint_provider():
    con = duckdb.connect(":memory:")
    _qualified_stock(con, "2026-09-29")
    con.execute("ALTER TABLE intraday_stock_flow_batch ADD COLUMN provider VARCHAR")
    con.execute("UPDATE intraday_stock_flow_batch SET provider='primary'")
    con.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN provider VARCHAR")
    con.execute("UPDATE multi_source_stock_flow SET provider='other'")
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday",
                                         required_groups=["stock_capital_flow"],
                                         now=datetime(2026, 9, 29, 10, 1))
    assert not result["source_ready"]
    assert result["groups"][0]["coverage"]["actual"]["observed_codes"] == 0
    con.close()


def test_metadata_not_yet_observed_cannot_qualify_an_as_of_review():
    con = duckdb.connect(":memory:")
    _qualified_stock(con, "2026-09-29")
    con.execute("ALTER TABLE intraday_stock_flow_batch ADD COLUMN updated_at TIMESTAMP")
    con.execute("UPDATE intraday_stock_flow_batch SET updated_at='2026-09-29 11:00:00'")
    gate = capital_flow_coverage(con, "2026-09-29", "stock", now=datetime(2026, 9, 29, 10, 1))
    assert not gate["passed"] and gate["reason"] == "batch_evidence_not_available_as_of"
    assert capital_flow_coverage(con, "2026-09-29", "stock")["passed"]
    con.close()


def test_auction_accepts_calendar_proven_previous_session_market_context(tmp_path):
    db_path = tmp_path / "auction-previous-session.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)")
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES "
        "('2026-07-23', true), ('2026-07-24', true)"
    )
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-23')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-24' trade_date")
    con.execute("CREATE TABLE auction_tick(trade_date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO auction_tick VALUES ('2026-07-24', '000001')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-24", "auction")

    assert result["ready"] is True
    market = next(item for item in result["groups"] if item["group"] == "market_state")
    assert market["status"] == "previous_session_context"
    assert market["context_trade_date"] == "2026-07-23"


def test_auction_rejects_arbitrary_old_market_row(tmp_path):
    db_path = tmp_path / "auction-old-context.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)")
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES "
        "('2026-07-23', true), ('2026-07-24', true)"
    )
    con.execute("CREATE TABLE daily_summary(date DATE)")
    con.execute("INSERT INTO daily_summary VALUES ('2026-07-15')")
    con.execute("CREATE VIEW v_limit_pool AS SELECT '2026-07-24' trade_date")
    con.execute("CREATE TABLE auction_tick(trade_date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO auction_tick VALUES ('2026-07-24', '000001')")
    con.close()

    result = assess_trade_date_readiness(db_path, "2026-07-24", "auction")

    assert result["ready"] is False
    assert "market_state" in result["missing_groups"]
