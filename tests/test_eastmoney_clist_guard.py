from __future__ import annotations

import pytest

from trade_system.eastmoney_clist_guard import EastmoneyClistGuard, EastmoneyClistUnavailable


def test_clist_guard_cools_down_and_success_resets(tmp_path):
    guard = EastmoneyClistGuard(tmp_path / "guard.json")
    state = guard.record_failure("RemoteDisconnected", endpoint="https://82.push2.eastmoney.com/api/qt/clist/get")
    assert state["failures"] == 1
    assert state["last_endpoint"].startswith("https://82.")
    with pytest.raises(EastmoneyClistUnavailable):
        guard.assert_available()
    state = guard.record_success("https://push2.eastmoney.com/api/qt/clist/get")
    assert state["failures"] == 0
    guard.assert_available()
