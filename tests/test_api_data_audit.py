import duckdb

from trade_system.api_data_audit import (
    ApiRequirement,
    build_gap_summary,
    count_existing_rows,
    materialize_params,
    summarize_payload,
)


def test_summarize_payload_counts_common_list_fields():
    summary = summarize_payload({"stock_code": "000001", "date": "2026-07-06", "ticks": []})

    assert summary.response_type == "dict"
    assert summary.top_level_keys == ["date", "stock_code", "ticks"]
    assert summary.item_count == 0
    assert summary.item_key == "ticks"

    summary = summarize_payload({"data": [{"code": "000001"}, {"code": "000002"}]})
    assert summary.item_count == 2
    assert summary.item_key == "data"


def test_build_gap_summary_classifies_api_available_and_reachable_empty():
    req = ApiRequirement(
        domain="auction",
        name="auction_tick",
        endpoint="/auction/tick",
        params={"code": "{stock_code}", "date": "{date}"},
        table="auction_tick",
        importance="core",
        access_method="GET /auction/tick?code=<stock_code>&date=<YYYY-MM-DD>",
        professional_use="auction confirmation",
    )

    available = build_gap_summary(req, http_status="ok", payload=summarize_payload({"ticks": [{"t": "09:25"}]}), table_rows=0)
    assert available.verdict == "api_available"
    assert available.api_item_count == 1

    empty = build_gap_summary(req, http_status="ok", payload=summarize_payload({"ticks": []}), table_rows=0)
    assert empty.verdict == "api_reachable_empty"
    assert "empty" in empty.note

    cached_empty = build_gap_summary(req, http_status="ok", payload=summarize_payload({"ticks": []}), table_rows=5)
    assert cached_empty.verdict == "api_reachable_empty"
    assert "local table already has rows" in cached_empty.note


def test_build_gap_summary_marks_derived_and_missing_local_sources():
    req = ApiRequirement(
        domain="index",
        name="index_kline",
        endpoint=None,
        params={},
        table="index_kline",
        importance="core",
        access_method="derived from /daily raw_json",
        professional_use="market filter",
        source_type="derived",
    )

    derived = build_gap_summary(req, http_status="not_probed", payload=None, table_rows=3)
    assert derived.verdict == "derived_available"

    missing = build_gap_summary(req, http_status="not_probed", payload=None, table_rows=0)
    assert missing.verdict == "derived_missing"


def test_materialize_params_replaces_known_placeholders():
    params = materialize_params(
        {"code": "{stock_code}", "date": "{date}", "ktype": "d"},
        date="2026-07-06",
        stock_code="000001",
        sector_code="801001",
    )

    assert params == {"code": "000001", "date": "2026-07-06", "ktype": "d"}


def test_count_existing_rows_reports_missing_tables(tmp_path):
    db_path = tmp_path / "audit.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO kline VALUES ('2026-07-06','000001')")
    con.close()

    assert count_existing_rows(db_path, "kline") == 1
    assert count_existing_rows(db_path, "auction_tick") is None
