from trade_system.api_endpoint_inventory import (
    EndpointCandidate,
    EndpointInventoryItem,
    classify_param_type,
    classify_probe_result,
    build_probe_params,
    infer_param_names,
    merge_candidates,
    normalize_endpoint,
    render_inventory_report,
    table_to_endpoint,
)


def test_normalize_endpoint_accepts_api_paths_and_rejects_project_paths():
    assert normalize_endpoint("/sector/strength?code=801001&date=2026-07-06") == "/sector/strength"
    assert normalize_endpoint("/advanced/bid-history") == "/advanced/bid-history"
    assert normalize_endpoint("/accio/stock_data/base.py") is None
    assert normalize_endpoint("/README.md") is None


def test_classify_param_type_identifies_common_shapes():
    assert classify_param_type([]) == "none"
    assert classify_param_type(["date"]) == "date"
    assert classify_param_type(["code"]) == "code"
    assert classify_param_type(["code", "date"]) == "code_date"
    assert classify_param_type(["start", "end"]) == "range"
    assert classify_param_type(["index", "page_size"]) == "pagination"
    assert classify_param_type(["codes", "date"]) == "batch"


def test_merge_candidates_combines_sources_params_and_table_names():
    items = merge_candidates(
        [
            EndpointCandidate("/sector/strength", "code", "collect_sector.py", {"code", "date"}, "sector_strength"),
            EndpointCandidate("/sector/strength", "doc", "trading_data_application.md", set(), None),
            EndpointCandidate("/sector/strength", "schema", "schema.py", set(), "sector_strength"),
        ]
    )

    assert len(items) == 1
    item = items[0]
    assert item.endpoint == "/sector/strength"
    assert item.param_type == "code_date"
    assert item.param_names == ["code", "date"]
    assert item.source_kinds == ["code", "doc", "schema"]
    assert item.table_name == "sector_strength"


def test_render_inventory_report_includes_required_buckets():
    item = merge_candidates(
        [
            EndpointCandidate("/auction/tick", "code", "collect_misc.py", {"code", "date"}, "auction_tick"),
        ]
    )[0]
    item.probe_status = "ok"
    item.item_count = 0
    item.verdict = "reachable_empty"
    item.note = "reachable but empty"

    report = render_inventory_report([item], date="2026-07-06", stock_code="000001", sector_code="801001")

    assert "Full API Endpoint Inventory" in report
    assert "Reachable But Empty" in report
    assert "/auction/tick" in report


def test_schema_table_exceptions_preserve_nested_api_paths():
    assert table_to_endpoint("l2_realtime_index_list") == "/l2/realtime/index-list"
    assert table_to_endpoint("l2_realtime_all_boards") == "/l2/realtime/all-boards"
    assert table_to_endpoint("market_emotion_money") == "/market/emotion-money-date"


def test_http_422_is_classified_as_parameter_uncertain():
    item = EndpointInventoryItem(
        endpoint="/advanced/zjmm-min",
        category="advanced",
        source_kinds=["schema"],
        source_files=["schema.py"],
        param_names=[],
        param_type="none",
        probe_params={},
    )
    item.probe_status = "http_422"

    verdict, note = classify_probe_result(item)

    assert verdict == "param_uncertain"
    assert "parameter" in note


def test_infer_params_for_schema_only_advanced_endpoints():
    assert infer_param_names("/advanced/trend-min", set()) == {"code", "date"}
    assert infer_param_names("/advanced/corporate-news", set()) == {"code"}
    assert infer_param_names("/advanced/turnover-ten", set()) == {"code"}
    assert infer_param_names("/topic/detail", set()) == {"topic_id"}


def test_build_probe_params_maps_plate_to_sector_code():
    params = build_probe_params(
        ["date", "plate", "topic_id"],
        date="2026-07-06",
        stock_code="603137",
        sector_code="801001",
    )

    assert params["plate"] == "801001"
    assert params["topic_id"] == "1"
