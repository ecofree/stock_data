import io
import json
import urllib.error

import pytest

from trade_system import data_store as base
from trade_system.source_validation import ValidationResult


def test_network_permission_error_opens_circuit(monkeypatch):
    monkeypatch.setattr(base.shared_host_limiter, 'acquire', lambda *a, **k: None)
    calls = []
    reason = OSError("socket access denied")
    reason.winerror = 10013

    def denied(*args, **kwargs):
        calls.append((args, kwargs))
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(base, "open_verified", denied)
    client = base.KPLClient(request_timeout=0.1, max_attempts=5, total_budget_seconds=5)

    assert client.get("/sector/capital", {"code": "801001"}) is None
    assert client.get("/sector/capital", {"code": "801002"}) is None
    assert len(calls) == 1
    assert client.stats["error"] == 1
    assert client.stats["circuit_open"] == 1
    assert client.stats["skipped"] == 1


def test_expired_total_budget_skips_request(monkeypatch):
    monkeypatch.setattr(
        base,
        "open_verified",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    client = base.KPLClient(total_budget_seconds=1)
    client._started_at -= 2

    assert client.get("/l2/stock-intraday") is None
    assert client.stats["circuit_open"] == 1
    assert client.stats["skipped"] == 1
    def exhausted(*args, **kwargs):
        assert kwargs['deadline'] > 0
        raise TimeoutError('shared rate limit deadline exhausted')
    monkeypatch.setattr(base.shared_host_limiter, 'acquire', exhausted)
    bounded = base.KPLClient(request_timeout=1, total_budget_seconds=3)
    assert bounded.get('/auction/tick', {'code': '000001', 'date': '2026-09-22'}) is None
    assert bounded.stats['rate_limited'] == 1


def test_dated_semantic_mismatch_does_not_retry(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"raw_data": [{"date": "2026-07-15"}]}'

    monkeypatch.setattr(base, "open_verified", lambda *args, **kwargs: (calls.append(1) or Response()))
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
        base,
        "open_verified",
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


def test_forbidden_optional_route_does_not_poison_core_client(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"data": [{"date": "2026-08-28"}]}'

    def open_route(request, **kwargs):
        calls.append(request.full_url)
        if "/auction/tick?" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)
        return Response()

    monkeypatch.setattr(base, "open_verified", open_route)
    client = base.KPLClient(max_attempts=1, total_budget_seconds=30)

    assert client.get("/auction/tick", {"code": "600519", "date": "2026-08-28"}) is None
    assert client.get("/daily", {"date": "2026-08-28"}) is not None
    assert client.stats["auth_error"] == 1
    assert client.stats["circuit_open"] == 0
    assert len(calls) == 2


def test_tick_decoded_observer_cannot_change_semantic_rejection(monkeypatch):
    calls = []
    observed = []
    payload = {"date": "20260707", "data": [{"time": "09:31", "order_id": "provider-order-A"}]}

    def response(*_args, **_kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def observer(decoded, metadata):
        observed.append((decoded, metadata))
        decoded["date"] = "20260706"
        metadata["request_params"]["date"] = "2026-07-07"

    monkeypatch.setattr(base, "open_verified", response)
    monkeypatch.setattr(base.shared_host_limiter, "acquire", lambda *_args, **_kwargs: None)
    client = base.KPLClient(max_attempts=5, total_budget_seconds=30)
    params = {"code": "000001", "date": "2026-07-06"}

    assert client.get("/l2/tick-orders", params, decoded_observer=observer) is None
    assert calls == [1]
    assert len(observed) == 1
    assert params["date"] == "2026-07-06"
    assert client.stats["semantic_error"] == 1
    assert client.stats["success"] == 0
    assert set(observed[0][1]) == {
        "endpoint", "request_params", "requested_at", "response_observed_at", "arrival_time_basis",
    }


def test_decoded_observer_is_explicitly_scoped_to_three_tick_endpoints(monkeypatch):
    def forbidden_request(*_args, **_kwargs):
        raise AssertionError("invalid observer must be rejected before a request")

    monkeypatch.setattr(base, "open_verified", forbidden_request)
    client = base.KPLClient(max_attempts=1)
    with pytest.raises(ValueError, match="scoped to the three L2 tick endpoints"):
        client.get("/market/rise-fall", {"date": "2026-07-06"}, decoded_observer=lambda *_args: None)
    with pytest.raises(TypeError, match="must be callable"):
        client.get("/l2/tick-history", {"date": "2026-07-06"}, decoded_observer=True)
