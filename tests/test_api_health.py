import pytest

from trade_system.api_health import require_api_key


def test_require_api_key_rejects_missing_or_placeholder_key():
    with pytest.raises(RuntimeError, match="KPL_API_KEY"):
        require_api_key("")
    with pytest.raises(RuntimeError, match="KPL_API_KEY"):
        require_api_key("replace-me")


def test_require_api_key_accepts_non_empty_key():
    assert require_api_key("abc123") == "abc123"
