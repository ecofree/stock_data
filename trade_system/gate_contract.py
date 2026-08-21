"""Canonical operator state for the daily trading-data pipeline.

The project has several useful gates, but they describe different things:
source freshness, data certification, capital-flow reconciliation, and entry
execution.  This module gives those dimensions one stable vocabulary while
keeping the legacy boolean aliases used by existing reports.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


OPERATOR_STATE_VERSION = "p0.operator_state.v1"


def build_operator_state(
    *,
    source_ready: bool,
    pipeline_ready: bool | None = None,
    artifact_current: bool = True,
    data_certified_ready: bool | None = None,
    flow_certified_ready: bool | None = None,
    execution_ready: bool = False,
    run_status: str = "not_run",
    blockers: Iterable[str] = (),
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the one operator-facing state envelope.

    ``flow_certified_ready`` is optional because the base readiness check does
    not run the independent capital-flow reconciliation.  An unassessed flow
    gate must never be presented as ``analysis_ready``: the returned
    ``analysis_scope`` makes that distinction explicit and keeps the legacy
    aliases fail-closed.  Once flow evidence is available, ``analysis_ready``
    is the conjunction of both domains.
    """
    source = bool(source_ready)
    pipeline = source if pipeline_ready is None else bool(pipeline_ready)
    artifact = bool(artifact_current)
    data_certified = (
        source and pipeline and artifact
        if data_certified_ready is None
        else bool(data_certified_ready)
    )
    flow_certified = (
        None if flow_certified_ready is None else bool(flow_certified_ready)
    )
    analysis_scope = (
        "certified"
        if flow_certified is True
        else "blocked"
        if flow_certified is False
        else "unassessed"
    )
    analysis_ready = data_certified and flow_certified is True
    blocker_list = sorted({str(item) for item in blockers if str(item)})
    warning_list = sorted({str(item) for item in warnings if str(item)})
    if blocker_list or not source or not pipeline or not artifact:
        operator_status = "blocked"
    elif bool(execution_ready) and analysis_ready:
        operator_status = "executable"
    elif analysis_ready:
        operator_status = "analysis_only"
    elif flow_certified is None and data_certified:
        operator_status = "data_only"
    else:
        operator_status = "uncertified"
    effective_execution_ready = bool(execution_ready) and analysis_ready
    return {
        "operator_state_version": OPERATOR_STATE_VERSION,
        "operator_status": operator_status,
        "run_status": str(run_status),
        "source_ready": source,
        "pipeline_ready": pipeline,
        "artifact_current": artifact,
        "data_certified_ready": data_certified,
        "flow_certified_ready": flow_certified,
        "analysis_ready": analysis_ready,
        "analysis_assessed": flow_certified is not None,
        "analysis_scope": analysis_scope,
        "execution_ready": effective_execution_ready,
        "blockers": blocker_list,
        "warnings": warning_list,
        # Compatibility aliases.  New code should use the explicit fields
        # above; these remain so older reports do not silently break.
        "ready": source,
        "certified_ready": analysis_ready,
        "analytics_ready": analysis_ready,
    }
