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
