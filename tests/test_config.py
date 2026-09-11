"""Notification config parsing."""
from __future__ import annotations

import pytest

from zwave_controller.config import Config, ConfigError

BASE_ENV = {
    "ZWAVE_WS_URL": "ws://localhost:3000",
    "KEYPAD_NODE_ID": "2",
    "UNIFI_HOST": "https://unifi.local/",
    "UNIFI_API_KEY": "key",
    "UNIFI_POLICY_ID": "policy",
    "DISARM_PIN": "1234",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "NTFY_URL",
        "NTFY_TOKEN",
        "NOTIFY_COOLDOWN_SECONDS",
        "NOTIFY_MOTION",
        "NOTIFY_BATTERY_THRESHOLD",
        "UNIFI_SITE",
        "UNIFI_VERIFY_TLS",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)


def test_notifications_disabled_by_default():
    cfg = Config.from_env()
    assert cfg.ntfy_url is None
    assert cfg.notifications_enabled is False


def test_notification_defaults(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/topic")
    cfg = Config.from_env()
    assert cfg.notifications_enabled is True
    assert cfg.notify_cooldown_seconds == 300
    assert cfg.notify_motion is True
    assert cfg.notify_battery_threshold == 20
    assert cfg.ntfy_token is None


def test_notification_overrides(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/topic")
    monkeypatch.setenv("NTFY_TOKEN", "tk_abc")
    monkeypatch.setenv("NOTIFY_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("NOTIFY_MOTION", "false")
    monkeypatch.setenv("NOTIFY_BATTERY_THRESHOLD", "35")
    cfg = Config.from_env()
    assert cfg.ntfy_token == "tk_abc"
    assert cfg.notify_cooldown_seconds == 60
    assert cfg.notify_motion is False
    assert cfg.notify_battery_threshold == 35


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NOTIFY_COOLDOWN_SECONDS", "soon"),
        ("NOTIFY_COOLDOWN_SECONDS", "-5"),
        ("NOTIFY_BATTERY_THRESHOLD", "101"),
        ("NOTIFY_MOTION", "maybe"),
    ],
)
def test_bad_values_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Config.from_env()
