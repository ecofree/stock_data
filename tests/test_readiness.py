import duckdb
import pytest
import json
import hashlib
from datetime import datetime

from trade_system.readiness import assess_trade_date_readiness, capital_flow_coverage, relation_freshness


def _qualified_prices(con, day):
    _qualified_stock(con, day, ("000001", "000002"))
    con.execute("CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)")
    con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE',?,true),('SZSE',?,true)", [day, day])
    con.execute("CREATE TABLE v_kline_daily(trade_date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,"
                "low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE,change_pct DOUBLE,provider VARCHAR,"
                "adjustment VARCHAR,volume_unit VARCHAR,amount_unit VARCHAR,fetched_at TIMESTAMP,is_fallback BOOLEAN)")
    con.execute("INSERT INTO v_kline_daily VALUES (?,'000001',10,12,9,11,100,1100,1,'xiaodefa',"
                "'none','shares','yuan',?,false)", [day, day + " 16:00:00"])


def test_price_facts_keep_full_denominator_without_borrowing_pb_or_independent_flow():
    from trade_system.readiness import market_view_capability
    day = "2026-09-29"
    with duckdb.connect(":memory:") as con:
        _qualified_prices(con, day)
        con.execute("CREATE TABLE tushare_daily_basic(trade_date DATE,stock_code VARCHAR,pb DOUBLE)")
        con.execute("INSERT INTO tushare_daily_basic VALUES (?,'000001',NULL)", [day])
        cap = market_view_capability(con, day, now=datetime(2026, 9, 29, 17))
        assert cap["ready"] and cap["expected_rows"] == 2 and cap["qualified_rows"] == 1
        assert cap["eligible_codes"] == ["000001"]
        assert cap["ineligible_codes"] == [{"stock_code": "000002", "reason": "price_missing"}]
        assert cap["breadth"] == {"rise": 1, "fall": 0, "flat": 0, "samples": 1}
        assert cap["input_received_at_min"] == cap["input_received_at_max"] == day + "T16:00:00"
        assert cap["scope_sha256"] and not cap["full_market_certified"]


@pytest.mark.parametrize("damage", ["unit", "future", "duplicate", "fallback", "identity", "calendar", "nonfinite"])
def test_market_capability_rejects_only_unqualified_price_inputs(damage):
    from trade_system.readiness import market_view_capability
    day = "2026-09-29"
    with duckdb.connect(":memory:") as con:
        _qualified_prices(con, day)
        if damage == "unit":
            con.execute("UPDATE v_kline_daily SET amount_unit='unknown'")
        elif damage == "future":
            con.execute("UPDATE v_kline_daily SET fetched_at='2026-09-29 18:00:00'")
        elif damage == "duplicate":
            con.execute("INSERT INTO v_kline_daily SELECT * FROM v_kline_daily")
        elif damage == "fallback":
            con.execute("UPDATE v_kline_daily SET is_fallback=true")
        elif damage == "identity":
            con.execute("UPDATE multi_source_observation SET payload_hash='changed'")
        elif damage == "calendar":
            con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE',?,false)", [day])
        else:
            con.execute("UPDATE v_kline_daily SET change_pct='NaN'")
        cap = market_view_capability(con, day, now=datetime(2026, 9, 29, 17))
        assert not cap["ready"] and not cap["eligible_codes"]


def test_missing_price_model_never_borrows_stage_readiness():
    from trade_system.readiness import price_research_capability
    result = price_research_capability(None, "2026-09-29", now=datetime(2026, 9, 29, 17))
    assert not result["ready"] and result["blockers"] == ["frozen_price_workspace_missing"]


def test_frozen_price_research_uses_actual_features_window_and_original_clocks(tmp_path, monkeypatch):
    """Synthetic boundary fixture; existing build/campaign tests cover their seals."""
    import pandas as pd
    from trade_system.readiness import price_research_capability
    from trade_system.v2 import research_product as product, research_campaign as campaign, research_dataset as dataset
    from trade_system.v2.domain import file_hash
    from trade_system.v2.gap_evidence import write_json
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-08-31", periods=22)]
    day = days[-1]
    rows = [dict(instrument="000001", datetime=d, open=10+i/10, high=12+i/10,
                 low=9+i/10, close=11+i/10, volume=100+i, turnover=1000+i,
                 net_mf_amount=None, receipt_files=dict(native="raw.json", daily="raw.json", adj_factor="raw.json"))
            for i, d in enumerate(days)]
    actual_frame = pd.DataFrame(rows)
    actual_frame["net_mf_amount"] = pd.to_numeric(actual_frame["net_mf_amount"], errors="coerce")
    actual = dataset.features(actual_frame, days).iloc[-1]
    write_json(tmp_path / "preprocessing.json", {"used_features": dataset.BASE})
    write_json(tmp_path / "configuration.json", {"universe": ["000001"]})
    write_json(tmp_path / "raw.json", {"received_at": day + "T16:10:00+08:00"})
    model = dict(model_id="frozen-fixture", preprocessing_path=str(tmp_path / "preprocessing.json"),
                 frozen_at=days[0] + "T16:00:00+08:00", train_end=days[0], inference_policy="price_21_sessions_v2")
    prediction = dict(model_id=model["model_id"], date=day, execution_ready=False,
        scope="actually_received_current_research_predictions_not_trading_signals", captured_at=day + "T17:00:00+08:00",
        feature_source_sha256=file_hash(dataset.__file__), receipt_folder=str(tmp_path),
        receipt_manifest_id="actual-fixture-manifest", prediction_id="frozen-prediction",
        rows=[dict(instrument="000001", prediction=0.5, features={c:float(actual[c]) for c in dataset.BASE})])
    reg = dict(origin="native_and_relay", config={"codes": ["000001.SZ"]})
    observed = dict(calendar={"SSE": days, "SZSE": days.copy()}, rows=rows,
                    receipt_manifest_id=prediction["receipt_manifest_id"])
    monkeypatch.setattr(product, "read_build", lambda _: (tmp_path, model, {}))
    monkeypatch.setattr(product, "read_prediction", lambda _: prediction)
    monkeypatch.setattr(campaign, "cached_replay", lambda _: (reg, {"raw.json":file_hash(tmp_path / "raw.json")}, {0:days}, []))
    monkeypatch.setattr(campaign, "derive", lambda *a, **kw: observed)
    result = price_research_capability(tmp_path, day, now=datetime.fromisoformat(day + "T18:00:00"))
    assert result["ready"] and result["eligible_codes"] == ["000001"]
    assert result["minimum_input_sessions"] == 21 and not result["execution_ready"]
    (tmp_path / "preprocessing.json").write_text(json.dumps({"used_features": dataset.BASE + dataset.MONEY}), encoding="utf-8")
    assert not price_research_capability(tmp_path, day, now=datetime.fromisoformat(day + "T18:00:00"))["ready"]
    (tmp_path / "preprocessing.json").write_text(json.dumps({"used_features": dataset.BASE}), encoding="utf-8")
    rows[-10]["close"] = None
    assert not price_research_capability(tmp_path, day, now=datetime.fromisoformat(day + "T18:00:00"))["ready"]
    rows[-10]["close"] = 11+12/10
    (tmp_path / "raw.json").write_text(json.dumps({"received_at": day + "T19:00:00+08:00"}), encoding="utf-8")
    assert not price_research_capability(tmp_path, day, now=datetime.fromisoformat(day + "T18:00:00"))["ready"]
    (tmp_path / "raw.json").write_text(json.dumps({"received_at": day + "T16:10:00+08:00"}), encoding="utf-8")
    write_json(tmp_path / "receipt-00.json", {"received_at": day + "T19:00:00+08:00"})
    reg["requests"] = [{"kind": "native_calendar"}]
    monkeypatch.setattr(campaign, "cached_replay", lambda _: (reg,
        {name:file_hash(tmp_path / name) for name in ("raw.json", "receipt-00.json")}, {0:days}, []))
    late_calendar = price_research_capability(tmp_path, day, now=datetime.fromisoformat(day + "T18:00:00"))
    assert not late_calendar["ready"]
    assert late_calendar["blockers"] == ["research_calendar_or_identity_arrived_after_as_of"]


def _batch_evidence(con, kind, day, expected=1, observed=1, coverage=100, status="success"):
    con.execute(f"CREATE TABLE IF NOT EXISTS intraday_{kind}_flow_batch("
                "trade_date DATE,expected_rows INTEGER,fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR)")
    con.execute(f"INSERT INTO intraday_{kind}_flow_batch VALUES (?,?,?,?,?)",
                [day, expected, observed, coverage, status])


def _qualified_stock(con, day, codes=("000001",)):
    con.execute("CREATE TABLE multi_source_stock_flow(source_date DATE,stock_code VARCHAR,"
                "main_net DOUBLE,fetched_at TIMESTAMP,provider VARCHAR,amount_unit VARCHAR,"
                "origin_provider VARCHAR,source_api VARCHAR,flow_definition VARCHAR,field_mapping_version VARCHAR)")
    for code in codes:
        con.execute("INSERT INTO multi_source_stock_flow VALUES (?,?,100,?,'xiaodefa_moneyflow_dc',"
                    "'yuan','eastmoney','moneyflow_dc','provider_main_orders_net','stock_flow_v3_explicit_units')",
                    [day, code, day + " 10:00:00"])
    _batch_evidence(con, "stock", day, len(codes), len(codes))
    con.execute("ALTER TABLE intraday_stock_flow_batch ADD COLUMN provider VARCHAR")
    con.execute("UPDATE intraday_stock_flow_batch SET provider='xiaodefa_moneyflow_dc'")
    expected = set(codes)
    while len(expected) < len(codes):
        expected.add(f"{len(expected)+1:06d}")
    con.execute("CREATE TABLE tushare_stock_basic(ts_code VARCHAR,stock_code VARCHAR,stock_name VARCHAR,"
                "area VARCHAR,industry VARCHAR,market VARCHAR,list_date DATE,delist_date DATE)")
    for code in sorted(expected):
        con.execute("INSERT INTO tushare_stock_basic VALUES (?,?,'fixture',NULL,NULL,NULL,'2000-01-01',NULL)",
                    [code + ".SZ", code])
    _seal_stock_reference(con, day)


def _receipt(con, day, kind, payload, *, observed=None):
    con.execute("CREATE TABLE IF NOT EXISTS multi_source_observation(source_date DATE,data_type VARCHAR,"
                "provider VARCHAR,status VARCHAR,observed_at TIMESTAMP,payload_json VARCHAR,payload_hash VARCHAR)")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    con.execute("INSERT INTO multi_source_observation VALUES (?,?,'xiaodefa','qualified',?,?,?)",
                [day, kind, observed or day + " 09:00:00", encoded, digest])
    return digest


def _seal_stock_reference(con, day):
    rows = con.execute("SELECT ts_code,stock_code,stock_name,area,industry,market,list_date,delist_date "
                       "FROM tushare_stock_basic ORDER BY ts_code").fetchall()
    version = hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()
    _receipt(con, day, "tushare_stock_basic_snapshot", dict(version=version, scope=["L", "D"],
             listing_membership=dict(as_of=day, not_listed=[], membership_only=[])))


def _qualified_sector(con, day):
    con.execute("CREATE TABLE multi_source_sector_flow(source_date DATE,sector_code VARCHAR,"
                "sector_type VARCHAR,main_net DOUBLE,amount_unit VARCHAR,fetched_at TIMESTAMP,provider VARCHAR,raw_json VARCHAR)")
    version = _receipt(con, day, "em_industry_catalogue", dict(api="dc_index", params=dict(trade_date=day.replace("-", "")),
        rows=[dict(ts_code="BK0001.DC", trade_date=day.replace("-", ""), idx_type="行业板块")]))
    from trade_system.flow_contract import normalize_sector_flow_row
    for code, kind, provider in [("BK0001", "em_industry", "eastmoney_sector_full"),
                                 ("THS-1", "ths_concept_derived", "derived_ths_stock_aggregate")]:
        row = dict(sector_type=kind, main_net=100, amount_unit="yuan", catalogue_version=version if kind == "em_industry" else day,
                   raw=dict(membership_snapshot_date=day, input_received_min=day + " 10:00:00", input_received_max=day + " 10:00:00"))
        encoded = json.dumps(dict(row, canonical_contract=normalize_sector_flow_row(row, provider)))
        con.execute("INSERT INTO multi_source_sector_flow VALUES (?,?,?,100,'yuan',?,?,?)",
                    [day, code, kind, day + " 10:00:00", provider, encoded])
    con.execute("CREATE TABLE ths_concept_daily(trade_date DATE,concept_code VARCHAR,stock_count INTEGER,date_verified BOOLEAN,raw_json VARCHAR)")
    con.execute("CREATE TABLE ths_concept_stock_history(trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,date_verified BOOLEAN,raw_json VARCHAR)")
    con.execute("CREATE TABLE ths_concept_member_checkpoint(trade_date DATE,concept_code VARCHAR,status VARCHAR)")
    con.execute("CREATE TABLE ths_concept_snapshot_expectation(trade_date DATE,expected_concepts INTEGER,status VARCHAR)")
    encoded = json.dumps(dict(fetched_date=day))
    con.execute("INSERT INTO ths_concept_daily VALUES (?,'THS-1',2,true,?)", [day, encoded])
    con.execute("INSERT INTO ths_concept_stock_history VALUES (?,'THS-1','000001',true,?),"
                "(?,'THS-1','000002',true,?)", [day, encoded, day, encoded])
    con.execute("INSERT INTO ths_concept_member_checkpoint VALUES (?,'THS-1','success')", [day])
    con.execute("INSERT INTO ths_concept_snapshot_expectation VALUES (?,1,'success')", [day])
    con.execute("CREATE VIEW v_sector_capital AS SELECT source_date trade_date,sector_code,"
                "main_net main_net_inflow,fetched_at FROM multi_source_sector_flow")
    _batch_evidence(con, "sector", day, 2, 2)
    con.execute("CREATE TABLE intraday_sector_flow_taxonomy(trade_date DATE,taxonomy VARCHAR,"
                "expected_rows INTEGER,fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR,provider VARCHAR,last_error VARCHAR)")
    con.execute("INSERT INTO intraday_sector_flow_taxonomy VALUES (?, 'em_industry',1,1,100,'success','eastmoney_sector_full',NULL),"
                "(?, 'ths_concept',1,1,100,'success','derived_ths_stock_aggregate',NULL)", [day, day])


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
    _qualified_stock(con, "2026-07-31")
    con.execute("ALTER TABLE multi_source_stock_flow ALTER fetched_at TYPE VARCHAR")
    con.execute("UPDATE multi_source_stock_flow SET fetched_at='2026-07-31 17:45:00'")
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
        con.execute("INSERT INTO tushare_stock_basic(ts_code,list_date,delist_date) SELECT 'old'||i,'2000-01-01','2026-07-01' FROM range(2000) t(i)")
        con.execute("INSERT INTO tushare_stock_basic(ts_code,list_date) VALUES ('new','2026-07-10')")
        con.execute("DELETE FROM multi_source_observation WHERE data_type='tushare_stock_basic_snapshot'")
        _seal_stock_reference(con, "2026-07-09")
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
    con.execute("UPDATE intraday_stock_flow_batch SET provider='primary'")
    con.execute("UPDATE multi_source_stock_flow SET provider='other'")
    result = assess_trade_date_readiness(con, "2026-09-29", "intraday",
                                         required_groups=["stock_capital_flow"],
                                         now=datetime(2026, 9, 29, 10, 1))
    assert not result["source_ready"]
    assert result["groups"][0]["coverage"]["actual"]["observed_codes"] == 0
    con.close()


@pytest.mark.parametrize("field,value", [("amount_unit", "unknown"), ("amount_unit", "10000_yuan"),
    ("flow_definition", "total_net_only"), ("origin_provider", "unknown"),
    ("source_api", "moneyflow"), ("field_mapping_version", "unreviewed")])
def test_same_count_does_not_certify_unknown_stock_measure(field, value):
    con = duckdb.connect(":memory:")
    _qualified_stock(con, "2026-09-29")
    con.execute(f'UPDATE multi_source_stock_flow SET "{field}"=?', [value])
    gate = capital_flow_coverage(con, "2026-09-29", "stock", require_actual=True, now=datetime(2026,9,29,10,1))
    assert not gate['passed'] and gate['actual']['observed_codes'] == 0
    con.close()


def test_same_count_wrong_security_and_changed_reference_fail_closed():
    con = duckdb.connect(":memory:")
    _qualified_stock(con, "2026-09-29")
    con.execute("UPDATE multi_source_stock_flow SET stock_code='000002'")
    gate = capital_flow_coverage(con, "2026-09-29", "stock", require_actual=True, now=datetime(2026,9,29,10,1))
    assert not gate['passed'] and gate['actual']['unexpected_codes'] == ['000002']
    con.execute("UPDATE multi_source_stock_flow SET stock_code='000001'")
    con.execute("UPDATE tushare_stock_basic SET stock_name='changed'")
    gate = capital_flow_coverage(con, "2026-09-29", "stock", require_actual=True, now=datetime(2026,9,29,10,1))
    assert not gate['passed'] and gate['actual']['reason'] == 'dated_stock_reference_unqualified'
    con.close()


@pytest.mark.parametrize("damage", ['unknown_unit', 'wrong_identity', 'wrong_definition', 'changed_catalogue', 'refreshed_derived_clock'])
def test_sector_count_cannot_replace_dated_scope_and_measure_evidence(damage):
    con = duckdb.connect(":memory:")
    _qualified_sector(con, "2026-09-29")
    if damage == 'unknown_unit':
        con.execute("UPDATE multi_source_sector_flow SET amount_unit='unknown' WHERE sector_type='em_industry'")
    elif damage == 'wrong_identity':
        con.execute("UPDATE multi_source_sector_flow SET sector_code='BK0002' WHERE sector_type='em_industry'")
    elif damage == 'wrong_definition':
        raw = json.loads(con.execute("SELECT raw_json FROM multi_source_sector_flow WHERE sector_type='em_industry'").fetchone()[0])
        raw['canonical_contract']['flow_definition'] = 'sector_total_net'
        con.execute("UPDATE multi_source_sector_flow SET raw_json=? WHERE sector_type='em_industry'", [json.dumps(raw)])
    elif damage == 'changed_catalogue':
        con.execute("UPDATE multi_source_observation SET payload_hash='changed' WHERE data_type='em_industry_catalogue'")
    else:
        con.execute("UPDATE multi_source_sector_flow SET fetched_at='2026-09-29 10:01:00' WHERE sector_type='ths_concept_derived'")
    gate = capital_flow_coverage(con, "2026-09-29", "sector", require_actual=True, now=datetime(2026,9,29,10,1))
    assert not gate['passed']
    con.close()


def test_sector_half_products_cannot_be_unioned_into_full_taxonomy():
    con = duckdb.connect(":memory:")
    day = '2026-09-29'
    _qualified_sector(con, day)
    con.execute("DELETE FROM multi_source_observation WHERE data_type='em_industry_catalogue'")
    version = _receipt(con, day, 'em_industry_catalogue', dict(api='dc_index',params=dict(trade_date='20260929'),
        rows=[dict(ts_code=c+'.DC',trade_date='20260929',idx_type='行业板块') for c in ['BK0001','BK0002']]))
    raw = json.loads(con.execute("SELECT raw_json FROM multi_source_sector_flow WHERE sector_type='em_industry'").fetchone()[0])
    raw['canonical_contract']['catalogue_version'] = version
    con.execute("UPDATE multi_source_sector_flow SET raw_json=? WHERE sector_type='em_industry'", [json.dumps(raw)])
    relay = dict(source_api='moneyflow_ind_dc',content_type='行业',ts_code='BK0002.DC',trade_date='20260929',unit='yuan',net_amount=100)
    con.execute("INSERT INTO multi_source_sector_flow VALUES (?,'BK0002.DC','em_industry',100,'yuan',?,'tushare_sector_full',?)",
                [day, day+' 10:00:00', json.dumps(relay)])
    con.execute("UPDATE intraday_sector_flow_taxonomy SET expected_rows=2,fetched_rows=2 WHERE taxonomy='em_industry'")
    con.execute("UPDATE intraday_sector_flow_batch SET expected_rows=3,fetched_rows=3")
    gate = capital_flow_coverage(con, day, 'sector', require_actual=True, now=datetime(2026,9,29,10,1))
    assert not gate['passed']
    assert gate['actual']['taxonomies']['em_industry']['observed_codes'] == 1
    assert gate['actual']['taxonomies']['em_industry']['coverage_pct'] == 50
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
