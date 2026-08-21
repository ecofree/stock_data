"""Tests for notify payload building and heartbeat liveness."""
from __future__ import annotations

import json

from trade_system import heartbeat, notify


def test_no_channels_configured(monkeypatch):
    for key in ("KPL_NOTIFY_WECHAT_WEBHOOK", "KPL_NOTIFY_DINGTALK_WEBHOOK",
                "KPL_NOTIFY_TELEGRAM_TOKEN", "KPL_NOTIFY_TELEGRAM_CHAT_ID",
                "KPL_NOTIFY_GENERIC_WEBHOOK"):
        monkeypatch.delenv(key, raising=False)
    assert notify.channel_payloads("t", "b") == {}
    # send_text must never raise even with nothing configured
    assert notify.send_text("t", "b")["none_configured"] == "ok"


def test_wechat_and_generic_payloads(monkeypatch):
    monkeypatch.setenv("KPL_NOTIFY_WECHAT_WEBHOOK", "https://example.test/wx")
    monkeypatch.setenv("KPL_NOTIFY_GENERIC_WEBHOOK", "https://example.test/hook")
    payloads = notify.channel_payloads("title", "body")
    assert payloads["wechat_work"][1] == {"msgtype": "text", "text": {"content": "title\nbody"}}
    assert payloads["generic"][1] == {"title": "title", "body": "body"}


def test_dingtalk_secret_signing(monkeypatch):
    webhook = "https://oapi.test/ding"
    secret = "SECtopsecret"
    monkeypatch.setenv("KPL_NOTIFY_DINGTALK_WEBHOOK", webhook)
    monkeypatch.setenv("KPL_NOTIFY_DINGTALK_SECRET", secret)
    url, payload = notify.channel_payloads("t", "b")["dingtalk"]
    assert url.startswith(webhook) and "timestamp=" in url and "sign=" in url
    assert payload["msgtype"] == "text"


def test_heartbeat_write_read_stale(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(heartbeat, "STATE_DIR", state_dir)
    path = heartbeat.write("unit", {"phase": "ferment"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == "unit" and "epoch" in data

    age = heartbeat.age_seconds("unit")
    assert age is not None and age < 60
    assert not heartbeat.is_stale("unit", max_age_seconds=3600)

    # Simulate staleness by rewinding the epoch.
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["epoch"] -= int(3 * 3600)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert heartbeat.is_stale("unit", max_age_seconds=2 * 3600)


def test_heartbeat_missing_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "absent")
    assert heartbeat.read("nope") is None
    assert heartbeat.age_seconds("nope") is None
    assert heartbeat.is_stale("nope")
