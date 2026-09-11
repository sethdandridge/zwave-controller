"""Environment-variable configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    zwave_ws_url: str
    keypad_node_id: int

    unifi_host: str
    unifi_site: str
    unifi_api_key: str
    unifi_policy_id: str
    unifi_verify_tls: bool

    disarm_pin: str
    log_level: str

    # Notifications. ``ntfy_url`` is the master switch: unset means the
    # sensor listener is never attached and nothing is ever sent.
    ntfy_url: str | None
    ntfy_token: str | None
    notify_cooldown_seconds: int
    notify_door_cooldown_seconds: int
    notify_motion: bool
    notify_battery_threshold: int

    @property
    def notifications_enabled(self) -> bool:
        return self.ntfy_url is not None

    @classmethod
    def from_env(cls) -> "Config":
        disarm_pin = _require("DISARM_PIN")
        if not disarm_pin.isdigit():
            raise ConfigError("DISARM_PIN must contain digits only")

        try:
            node_id = int(_require("KEYPAD_NODE_ID"))
        except ValueError as exc:
            raise ConfigError("KEYPAD_NODE_ID must be an integer") from exc

        battery_threshold = _int("NOTIFY_BATTERY_THRESHOLD", 20)
        if battery_threshold > 100:
            raise ConfigError("NOTIFY_BATTERY_THRESHOLD must be 0-100")

        return cls(
            zwave_ws_url=_require("ZWAVE_WS_URL"),
            keypad_node_id=node_id,
            unifi_host=_require("UNIFI_HOST").rstrip("/"),
            unifi_site=os.environ.get("UNIFI_SITE", "default").strip() or "default",
            unifi_api_key=_require("UNIFI_API_KEY"),
            unifi_policy_id=_require("UNIFI_POLICY_ID"),
            unifi_verify_tls=_bool("UNIFI_VERIFY_TLS", default=True),
            disarm_pin=disarm_pin,
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            ntfy_url=_optional("NTFY_URL"),
            ntfy_token=_optional("NTFY_TOKEN"),
            notify_cooldown_seconds=_int("NOTIFY_COOLDOWN_SECONDS", 300),
            notify_door_cooldown_seconds=_int("NOTIFY_DOOR_COOLDOWN_SECONDS", 15),
            notify_motion=_bool("NOTIFY_MOTION", default=True),
            notify_battery_threshold=battery_threshold,
        )
