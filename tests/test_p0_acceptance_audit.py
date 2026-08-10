from scripts.audit_p0_p3_acceptance import _normalize_trade_date


def test_acceptance_audit_normalizes_compact_trade_date():
    assert _normalize_trade_date("20260807") == "2026-08-07"
    assert _normalize_trade_date("2026-08-07") == "2026-08-07"
