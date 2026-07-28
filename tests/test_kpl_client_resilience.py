import urllib.error

import base
from trade_system.source_validation import ValidationResult


def test_network_permission_error_opens_circuit(monkeypatch):
    calls = []
    reason = OSError("socket access denied")
    reason.winerror = 10013

    def denied(*args, **kwargs):
        calls.append((args, kwargs))
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(base.urllib.request, "urlopen", denied)
    client = base.KPLClient(request_timeout=0.1, max_attempts=5, total_budget_seconds=5)

    assert client.get("/sector/capital", {"code": "801001"}) is None
    assert client.get("/sector/capital", {"code": "801002"}) is None
    assert len(calls) == 1
    assert client.stats["error"] == 1
    assert client.stats["circuit_open"] == 1
    assert client.stats["skipped"] == 1


def test_expired_total_budget_skips_request(monkeypatch):
    monkeypatch.setattr(
        base.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    client = base.KPLClient(total_budget_seconds=1)
    client._started_at -= 2

    assert client.get("/l2/stock-intraday") is None
    assert client.stats["circuit_open"] == 1
    assert client.stats["skipped"] == 1


def test_dated_semantic_mismatch_does_not_retry(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"raw_data": [{"date": "2026-07-15"}]}'

    monkeypatch.setattr(base.urllib.request, "urlopen", lambda *args, **kwargs: (calls.append(1) or Response()))
    monkeypatch.setattr(
        base,
        "validate_kpl",
        lambda endpoint, params, data: ValidationResult(False, "date mismatch requested=2026-07-16 returned=['2026-07-15']"),
    )
    client = base.KPLClient(max_attempts=5, total_budget_seconds=30)

    assert client.get("/market/rise-fall", {"date": "2026-07-16"}) is None
    assert len(calls) == 1
    assert client.stats["semantic_error"] == 1


def test_rise_fall_source_date_can_be_explicitly_preserved(monkeypatch):
    calls = []
    payload = {"raw_data": [[14, 2, 3, 11, 21.4, 2, "2026-07-15"]]}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return (
                b'{"raw_data": [[14, 2, 3, 11, 21.4, 2, "2026-07-15"]]}'
            )

    monkeypatch.setattr(
        base.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (calls.append(1) or Response()),
    )
    client = base.KPLClient(max_attempts=5, total_budget_seconds=30)

    assert client.get(
        "/market/rise-fall",
        {"date": "2026-07-16"},
        accept_source_date=True,
    ) == payload
    assert len(calls) == 1
    assert client.stats["success"] == 1
    assert client.stats["semantic_error"] == 1
