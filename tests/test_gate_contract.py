from trade_system.gate_contract import build_operator_state


def test_operator_state_is_data_scoped_until_flow_certification_is_assessed():
    state = build_operator_state(
        source_ready=True,
        data_certified_ready=True,
        flow_certified_ready=None,
    )

    assert state["data_certified_ready"] is True
    assert state["flow_certified_ready"] is None
    assert state["analysis_ready"] is False
    assert state["analysis_assessed"] is False
    assert state["analysis_scope"] == "unassessed"
    assert state["certified_ready"] is False
    assert state["operator_status"] == "data_only"


def test_operator_state_requires_flow_certification_when_present():
    state = build_operator_state(
        source_ready=True,
        data_certified_ready=True,
        flow_certified_ready=False,
    )

    assert state["data_certified_ready"] is True
    assert state["flow_certified_ready"] is False
    assert state["analysis_ready"] is False
    assert state["certified_ready"] is False
    assert state["operator_status"] == "uncertified"


def test_domain_uses_do_not_upgrade_strict_operator_aliases():
    from trade_system.gate_contract import build_operational_capabilities
    capabilities = build_operational_capabilities(trade_date="2026-09-29", as_of="2026-09-29T17:00:00",
        market_view={"ready": True}, price_research={"ready": True},
        stock_observation={"ready": True}, sector_observation={"ready": True})
    assert capabilities["schema"] == "operational_capabilities_v1"
    assert all(capabilities["capabilities"][name]["ready"] for name in
               ("market_view", "price_research", "flow_observation"))
    assert not capabilities["capabilities"]["flow_confirmation"]["ready"]
    assert not capabilities["execution_ready"]
    state = build_operator_state(source_ready=True, flow_certified_ready=False, execution_ready=True)
    for alias in ("analysis_ready", "certified_ready", "analytics_ready", "execution_ready"):
        assert state[alias] is False


def test_missing_or_non_boolean_domain_evidence_never_grants_capability():
    from trade_system.gate_contract import build_operational_capabilities
    result = build_operational_capabilities(trade_date="2026-09-29", as_of="unassessed",
                                          market_view={"ready": "true"})
    assert not any(item["ready"] for item in result["capabilities"].values())
