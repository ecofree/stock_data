"""V1 success expectations are retired; V2 owns limit/settlement invariants."""
import pytest
from trade_system import paper_execution


@pytest.mark.parametrize("method", ["submit_paper_order", "simulate_fill", "release_t1_sellable", "ensure_paper_tables"])
def test_legacy_paper_approval_cannot_restore_writer(tmp_path, method):
    target = tmp_path/"paper.duckdb"
    with pytest.raises(RuntimeError, match="retired"):
        getattr(paper_execution, method)(target, risk_approved=True, tradable=True)
    assert not target.exists()
